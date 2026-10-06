"""
label_cropper.py - crop shipping labels out of a Flipkart order PDF and
build packing list CSVs. (pymupdf-based version)

This is a refactored, importable version of the original standalone
script: the same label-detection and parsing logic, but exposed as a
single function (crop_labels) that takes a PDF path, mode, and output
directory - no hardcoded paths, no module-level settings to edit, no
os.chdir(), no sys.exit() (raises exceptions instead, so a FastAPI
endpoint can turn them into proper HTTP error responses).

crop_labels() returns a list of the output file paths it created:
  labels_thermal.pdf and/or labels_a4.pdf   (per `mode`)
  orders.csv        one row per item
  sku_summary.csv    total quantity ordered per SKU
  sku_units.csv      total pieces per SKU (quantity x pack size)
"""

import csv
import logging
import re
from collections import OrderedDict
from pathlib import Path

try:
    import pymupdf
except ImportError:                 # older PyMuPDF versions
    import fitz as pymupdf

logger = logging.getLogger("label-cropper")

MM = 72 / 25.4            # points per millimetre
A4 = (595.276, 841.89)    # points

# Fine-tuning settings kept as constants (not exposed to the user) -
# these matched the original script's defaults.
THERMAL_SIZE = "4x6"        # "WxH" inches, "WxHmm", or "auto"
A4_LABEL_SIZE = "fit"       # "original" or "fit"
A4_POSITION = "center"      # "top" or "center"
THERMAL_MARGIN_MM = 2
A4_MARGIN_MM = 10
PAD_PT = 3


class LabelCropError(Exception):
    """Raised when the uploaded PDF has no recognizable labels."""


# --------------------------------------------------------------------------
# Finding the label on a page
# --------------------------------------------------------------------------
def _detect_label_box(page, words):
    """
    Rectangle covering everything above the dashed 'cut here' line (or all
    content on the page if there is no such line). None for a blank page.
    """
    drawings = page.get_drawings()
    cut = None
    for d in drawings:
        dashes = (d.get("dashes") or "").replace(" ", "")
        r = d["rect"]
        if (dashes and not dashes.startswith("[]") and r.height < 2
                and r.width > 0.6 * page.rect.width):
            cut = r.y0 if cut is None else min(cut, r.y0)
    limit = cut - 1 if cut is not None else page.rect.height

    rects = [d["rect"] for d in drawings]
    rects += [pymupdf.Rect(w[:4]) for w in words]
    rects += [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()]
    rects = [r for r in rects if r.y1 <= limit]
    if not rects:
        return None
    box = pymupdf.Rect(min(r.x0 for r in rects) - PAD_PT,
                       min(r.y0 for r in rects) - PAD_PT,
                       max(r.x1 for r in rects) + PAD_PT,
                       min(limit, max(r.y1 for r in rects) + PAD_PT))
    return box & page.rect


# --------------------------------------------------------------------------
# Reading order details from the label
# --------------------------------------------------------------------------
def _group_lines(words, tol=3):
    """Group words (x0, y0, x1, y1, text) into lines, top to bottom."""
    lines = []
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        if lines and abs(w[1] - lines[-1][0][1]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(l, key=lambda w: w[0]) for l in lines]


def _parse_label(words):
    """
    words: the words inside the label. Returns (awb, order_id, items) where
    items is a list of {"sku_id", "description", "qty"} from the SKU table.
    """
    text = " ".join(w[4] for w in words)
    m = re.search(r"\bOD\d{12,}\b", text)
    order_id = m.group(0) if m else ""
    m = re.search(r"\bFMP[A-Z]\d{6,}\b", text)
    awb = m.group(0) if m else ""

    items = []
    qty_head = next((w for w in words if w[4].upper() == "QTY"), None)
    if qty_head is None:
        return awb, order_id, items

    qty_x = qty_head[0] - 6
    below = [w for w in words if w[1] > qty_head[3] - 1]
    for line in _group_lines(below):
        left = " ".join(w[4] for w in line if w[0] < qty_x)
        right = [w[4] for w in line if w[0] >= qty_x]
        if not awb and re.fullmatch(r"[A-Z0-9]{8,}", left) and not right:
            awb = left
        if "Not for resale" in left or left == awb:
            break
        if right and right[0].isdigit():
            left = re.sub(r"^\d+\s+", "", left)
            sku, _, desc = left.partition("|")
            items.append({"sku_id": sku.strip(), "description": desc.strip(),
                          "qty": int(right[0])})
        elif items and left:
            items[-1]["description"] = (items[-1]["description"] + " " + left).strip()
    return awb, order_id, items


# --------------------------------------------------------------------------
# Building the label PDFs
# --------------------------------------------------------------------------
def _parse_size(text):
    """'4x6' (inches), '100x150' (mm) or 'auto' -> points, or None for auto."""
    text = text.lower().strip()
    if text == "auto":
        return None
    w, h = (float(v) for v in text.split("x"))
    if w <= 10 and h <= 10:
        return w * 72, h * 72
    return w * MM, h * MM


def _target_rect(box, page_w, page_h, margin, upscale=True, valign="center"):
    """Where the label goes on the output page (aspect ratio kept)."""
    sw, sh = page_w - 2 * margin, page_h - 2 * margin
    scale = min(sw / box.width, sh / box.height)
    if not upscale:
        scale = min(scale, 1.0)
    w, h = box.width * scale, box.height * scale
    x = margin + (sw - w) / 2
    y = margin + (0 if valign == "top" else (sh - h) / 2)
    return pymupdf.Rect(x, y, x + w, y + h)


def _add_label(out, mode, src, page_no, box):
    if mode == "thermal":
        size = _parse_size(THERMAL_SIZE)
        if size is None:
            w, h = box.width, box.height
            rect = pymupdf.Rect(0, 0, w, h)
        else:
            w, h = size
            rect = _target_rect(box, w, h, THERMAL_MARGIN_MM * MM)
    else:  # a4: one label per sheet
        w, h = A4
        rect = _target_rect(box, w, h, A4_MARGIN_MM * MM,
                            upscale=(A4_LABEL_SIZE.lower() == "fit"),
                            valign=A4_POSITION.lower())
    page = out.new_page(width=w, height=h)
    # vector copy of just the label area; fonts/images are shared, not duplicated
    page.show_pdf_page(rect, src, page_no, clip=box)


def _process(pdf_path: Path, modes: list[str], out_dir: Path):
    """
    Crop every label in the PDF into the output PDF(s), one pass over the
    source handles all requested modes at once. Returns (rows, output_paths).
    """
    outputs = {m: pymupdf.open() for m in modes}
    rows = []
    count = 0

    src = pymupdf.open(str(pdf_path))
    for i, page in enumerate(src):
        words = page.get_text("words")
        box = _detect_label_box(page, words)
        if box is None or box.is_empty:
            logger.info("Skipped %s page %d: nothing found", pdf_path.name, i + 1)
            continue
        count += 1
        for mode, out in outputs.items():
            _add_label(out, mode, src, i, box)

        inside = [w for w in words if pymupdf.Rect(w[:4]).intersects(box)]
        awb, order_id, items = _parse_label(inside)
        if not items:
            logger.warning("No SKU rows read from %s page %d", pdf_path.name, i + 1)
        for it in items:
            rows.append({"awb_id": awb, "order_id": order_id,
                         "source_file": pdf_path.name, **it})
    # src must stay open (referenced above) until outputs are saved below -
    # show_pdf_page() keeps a live reference to it.

    if not count:
        raise LabelCropError("No shipping labels were found in this PDF.")

    output_paths = []
    for mode, out in outputs.items():
        out_path = out_dir / f"labels_{mode}.pdf"
        out.save(str(out_path), garbage=3, deflate=True)
        logger.info("Wrote %s (%d page(s))", out_path, out.page_count)
        output_paths.append(out_path)

    return rows, output_paths


# --------------------------------------------------------------------------
# CSV reports
# --------------------------------------------------------------------------
def _pack_size(sku_id):
    """Number at the end of the SKU ID, e.g. 'buck2' -> 2. No number -> 1."""
    m = re.search(r"(\d+)$", sku_id.strip())
    return int(m.group(1)) if m and int(m.group(1)) > 0 else 1


def _write_csv(path: Path, header, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    logger.info("Wrote %s (%d row(s))", path, len(rows))


def _write_reports(rows, out_dir: Path) -> list[Path]:
    orders_path = out_dir / "orders.csv"
    _write_csv(orders_path,
               ["awb_id", "order_id", "sku_id", "description", "qty", "source_file"],
               [[r["awb_id"], r["order_id"], r["sku_id"], r["description"],
                 r["qty"], r["source_file"]] for r in rows])

    totals = OrderedDict()
    for r in rows:
        t = totals.setdefault(r["sku_id"], {"description": r["description"],
                                            "orders": 0, "qty": 0})
        t["orders"] += 1
        t["qty"] += r["qty"]
    skus = sorted(totals.items(), key=lambda kv: -kv[1]["qty"])

    summary_path = out_dir / "sku_summary.csv"
    _write_csv(summary_path,
               ["sku_id", "description", "orders", "total_qty"],
               [[s, t["description"], t["orders"], t["qty"]] for s, t in skus])
    
    
    '''
    units_path = out_dir / "sku_units.csv"
    _write_csv(units_path,
               ["sku_id", "description", "pack_size", "total_qty", "total_units"],
               [[s, t["description"], _pack_size(s), t["qty"], t["qty"] * _pack_size(s)]
                for s, t in skus])
    '''
    #return [orders_path, summary_path, units_path]
    return [summary_path]


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def crop_labels(pdf_path: Path, mode: str, out_dir: Path) -> list[Path]:
    """
    Process a single Flipkart order PDF and write label PDF(s) + CSV
    reports into out_dir (must already exist). Returns the list of
    output file paths created.

    mode: "thermal" | "a4" | "both"
    """
    mode = mode.lower().strip()
    if mode not in ("thermal", "a4", "both"):
        raise ValueError('mode must be "thermal", "a4", or "both"')

    modes = ["thermal", "a4"] if mode == "both" else [mode]
    rows, output_files = _process(Path(pdf_path), modes, out_dir)
    output_files.extend(_write_reports(rows, out_dir))
    return output_files