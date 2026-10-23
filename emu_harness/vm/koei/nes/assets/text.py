#!/usr/bin/env python3
"""
text.py - KOEI font/text asset layer (locate font CHR + char->tile map -> nes-render).

Sibling to portrait.py: it owns the KOEI-specific knowledge -- WHERE the built-in game font lives in
the ROM and HOW an ASCII byte maps to a font tile -- and delegates ALL pixel work (tile decode,
palette, grid) to the generic `nes-render`. The eviction: NA1's render-text.py carried its own
`_tile_bitmap` (a hand-rolled 2bpp decoder) and its own pixel loop; those are gone here, replaced by
`NR.decode_tile` / `NR.render_tiles`.

The KOEI text pipeline (NA1, from render-text.py / ROADMAP): char_classify ($CDCA) maps an ASCII byte
to a pattern-table tile index as roughly `tile = ascii - 40`, with a per-range offset because the font
omits some punctuation between the digit/upper/lower runs. The glyph BITMAPS are CHR split across two
banks -- bank0 $B2BA holds tiles 0..80, bank6 $8264 holds the high tiles (lowercase tail + symbols)
starting at tile 77. We assemble those two slices into ONE font-CHR blob whose tile index == the
game's font tile number, so a built nametable of char-tile indices renders directly.

Config is per-title (NA1 here; a new title with a built-in font adds its own block). Dependency
direction: koei-nes -> nes-render (generic), never the reverse.
"""
import sys
from pathlib import Path

from emu_harness.machines.nes import ppu as NR   # the generic NES pixels
from emu_harness.vm.koei.nes.assets import MASTER
from emu_harness.vm.koei.nes.titles import game_cfg

# ---- per-title font config (NA1; from render-text.py / project ROADMAP) ----
# Two CHR slices that together cover the font tile range; `tile0` is the font tile index the slice
# begins at, so we can lay them into one blob at the right offset. `char_base` = char_classify's
# `tile = ascii - char_base`. Per-range offsets + explicit punctuation live in the maps below.
NA1 = {
    "font_slices": [
        # (cpu_bank, cpu_addr, first_font_tile, n_tiles)
        (0, 0xB2BA, 0, 81),     # tiles 0..80   (primary: digits / upper / common punct)
        (6, 0x8264, 77, 177),   # tiles 77..253 (lowercase tail + symbols)
    ],
    "ranges": [                 # (ascii_lo, ascii_hi, tile = ascii - offset)
        (48, 57, 40),           # '0'-'9' -> tile c-40  (digits start at tile 8)
        (65, 90, 43),           # 'A'-'Z' -> tile c-43
        (97, 122, 47),          # 'a'-'z' -> tile c-47
    ],
    "punct": {                  # tiles read straight off the $B2BA glyph grid
        '/': 7, '(': 3, ')': 4, '-': 5, '.': 6, ':': 18, '?': 21, '@': 22,
        "'": 50, '#': 76, '%': 77, '*': 78, '+': 79, ',': 80, '!': 1,
    },
    "fg": 0x30,                 # NES master index for ink (white); bg/transparent handled below
    "bg": 0x0D,                 # blank / background index
}


def _prg(cfg):
    rom = cfg["_rom"].read_bytes()
    return rom[16:] if rom[:4] == b"NES\x1a" else rom


def assemble_font_chr(prg, cfg=NA1):
    """KOEI-specific: stitch the two ROM CHR slices into one blob where blob_tile_index == font tile.

    Returns (chr_blob, n_tiles, blank_idx). The slices may overlap at the seam (bank0 ends at tile 80,
    bank6 starts at tile 77); bank6 wins on overlap, matching the game's CHR-RAM upload order. A
    synthetic all-zero tile is appended (font tile 0 is a SOLID fill, not blank) so space / unmapped
    chars have a true blank to point at -- the game draws space by not-drawing; here every nametable
    cell is drawn, so the blank must exist in CHR."""
    slices = cfg["font_slices"]
    ntiles = max(t0 + n for (_b, _a, t0, n) in slices)
    blob = bytearray(ntiles * 16)
    for (bank, addr, tile0, n) in slices:
        off = bank * 0x4000 + (addr & 0x3FFF)
        blob[tile0 * 16: (tile0 + n) * 16] = prg[off: off + n * 16]
    blank_idx = ntiles
    blob += bytes(16)                          # appended all-zero (blank) tile
    return bytes(blob), ntiles + 1, blank_idx


def char_to_tile(ch, cfg=NA1):
    """char_classify: ASCII char -> font tile index (None = render blank, e.g. space)."""
    c = ord(ch)
    if c == 32:                                 # space -> blank
        return None
    for lo, hi, offset in cfg["ranges"]:
        if lo <= c <= hi:
            return c - offset
    return cfg["punct"].get(ch, None)


def _pal16(cfg, fg, bg):
    """sub-palette 0 = [bg, ink, ink, ink] so any non-zero CHR pixel is ink (the game treats the font
    as 1bpp ink-on-blank). 16-entry palette16 for NR.render_tiles."""
    return [bg, fg, fg, fg] + [0] * 12


def render_text(text, *, game="na1", cfg=NA1, scale=4, fg=None, bg=None):
    """Render a single line of game-font text via the GENERIC renderer.

    Builds a 1-row nametable of the chars' font-tile indices and hands it to NR.render_tiles. `fg`/`bg`
    are NES master indices (default white-on-bg from cfg). Space / unmapped chars use the synthetic
    blank tile (font tile 0 is a solid fill, not blank)."""
    prg = _prg(game_cfg(game))
    chr_blob, _n, blank = assemble_font_chr(prg, cfg)
    fg = cfg["fg"] if fg is None else fg
    bg = cfg["bg"] if bg is None else bg
    nametable = [(t if (t := char_to_tile(ch, cfg)) is not None else blank) for ch in text]
    if not nametable:
        nametable = [blank]
    return NR.render_tiles(chr_blob, nametable, _pal16(cfg, fg, bg),
                           cols=len(nametable), rows=1, scale=scale, master=MASTER)


def font_atlas(game="na1", out="text_atlas.png", cfg=NA1, scale=5):
    """Deliverable: the whole printable glyph run (ASCII 40-122) as a verification atlas, via nes-render."""
    line = "".join(chr(c) for c in range(40, 123))
    img = render_text(line, game=game, cfg=cfg, scale=scale)
    img.save(out)
    print(f"wrote {out}  ({len(line)} glyphs, ASCII 40-122, via nes-render)")
    return img


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "atlas":
        font_atlas(out=sys.argv[2] if len(sys.argv) > 2 else "text_atlas.png")
    elif len(sys.argv) > 1:
        s = sys.argv[1]
        out = sys.argv[2] if len(sys.argv) > 2 else "text.png"
        render_text(s).save(out)
        print(f"wrote {out}  ('{s}', via nes-render)")
    else:
        print(__doc__)
