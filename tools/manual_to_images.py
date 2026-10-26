#!/usr/bin/env python3
"""
manual_to_images.py - convert a scanned game-manual PDF into per-page JPEGs (game-agnostic).

The digitpress NES manuals are scanned IMAGES (no text layer), so WebFetch/pdf text extraction
returns nothing. This renders each page to a JPEG so the pages can be read visually (or OCR'd),
turning the manual into a usable grounding source for the specific data-naming pass.

Two modes:
  render  (default) - rasterize each PAGE to a JPEG at --dpi (robust; one file per page)
  embed             - extract the embedded scan IMAGES verbatim (no re-encode; may be 1+ per page)

Usage:
  py -3.14 tools/manual_to_images.py <in.pdf> <out_dir> [--dpi 200] [--embed]

Needs PyMuPDF (`pip install -e ".[manual]"`). Was koei-nes `tools/manual_to_images.py` (moved
2026-10-26; koei-nes keeps an alias). PHASES.md phase 3: the manual is the schema oracle.
"""
import sys, argparse
from pathlib import Path
import fitz  # PyMuPDF

def render(doc, out, dpi):
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    written = []
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=mat)
        p = out / f"page_{i+1:03d}.jpg"
        pix.save(p, jpg_quality=85)
        written.append((p, pix.width, pix.height))
    return written

def embed(doc, out):
    written = []
    for i, page in enumerate(doc):
        for j, img in enumerate(page.get_images(full=True)):
            xref = img[0]
            d = doc.extract_image(xref)
            ext = d.get("ext", "png")
            p = out / f"page_{i+1:03d}_img{j+1}.{ext}"
            p.write_bytes(d["image"])
            written.append((p, d.get("width"), d.get("height")))
    return written

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf"); ap.add_argument("out_dir")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--embed", action="store_true")
    a = ap.parse_args()
    out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(a.pdf)
    print(f"{a.pdf}: {doc.page_count} pages")
    w = embed(doc, out) if a.embed else render(doc, out, a.dpi)
    for p, ww, hh in w:
        print(f"  {p.name}  {ww}x{hh}  {p.stat().st_size//1024}KB")
    print(f"wrote {len(w)} image(s) -> {out}")

if __name__ == "__main__":
    main()
