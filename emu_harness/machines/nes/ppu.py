#!/usr/bin/env python3
"""
Moved from the nes-render repo (emu-harness MIGRATION.md row O), which keeps an alias.

nes_render.py - a GENERIC NES PPU renderer: you hand it the planes, it gives you pixels.

This is the bottom layer of the asset stack (generic NES hardware; below the KOEI engine layer).
Nothing here is game-specific -- it's the standard NES PPU: 2bpp CHR tiles, the 64-colour master
palette, attribute-table sub-palette selection, nametable tile grids, and OAM sprites. Locating /
decoding the planes out of a particular ROM is the caller's job (the KOEI / game layer).

Design principle (learned from the NA1 tools): **the canvas is not fixed to 256x240.** You pass the
tile grid dimensions, so you can clip to a sub-region, or concatenate several nametable pages into one
flat index list and render them as a single wide/tall image -- whatever you give it, it renders.

API:
  decode_tile(chr, idx, base=0)                 -> 8x8 grid of 0..3 colour indices
  attr_subpalette(attr, col, row, attr_cols=8)  -> 0..3 sub-palette id for a tile cell
  render_tiles(chr, nametable, palette16, *, cols, rows, attr=None, chr_base=0, scale=1) -> PIL.Image
  render_sprites(chr, oam, palette16, base, *, chr_base=0, scale=1)                      -> PIL.Image
  render_chr_atlas(chr, *, count=256, cols=16, palette16=None, chr_base=0, scale=3)      -> PIL.Image

`chr` is raw CHR bytes (16 bytes/tile, 2bpp); `palette16` is 16 NES colour indices (4 sub-palettes x 4).
"""
from PIL import Image

# Standard NES master palette (64 colours -> RGB). Shared verbatim with the NA1 render tools.
NES_MASTER = [
    (84,84,84),(0,30,116),(8,16,144),(48,0,136),(68,0,100),(92,0,48),(84,4,0),(60,24,0),
    (32,42,0),(8,58,0),(0,64,0),(0,60,0),(0,50,60),(0,0,0),(0,0,0),(0,0,0),
    (152,150,152),(8,76,196),(48,50,236),(92,30,228),(136,20,176),(160,20,100),(152,34,32),(120,60,0),
    (84,90,0),(40,114,0),(8,124,0),(0,118,40),(0,102,120),(0,0,0),(0,0,0),(0,0,0),
    (236,238,236),(76,154,236),(120,124,236),(176,98,236),(228,84,236),(236,88,180),(236,106,100),(212,136,32),
    (160,170,0),(116,196,0),(76,208,32),(56,204,108),(56,180,204),(60,60,60),(0,0,0),(0,0,0),
    (236,238,236),(168,204,236),(188,188,236),(212,178,236),(236,174,236),(236,174,212),(236,180,176),(228,196,144),
    (204,210,120),(180,222,120),(168,226,144),(152,226,180),(160,214,228),(160,162,160),(0,0,0),(0,0,0),
]
# Warmer 2C02 approximation (FCEUX/Nestopia-style) -- matches the look of real KOEI screenshots and the
# original NA1 render tools, where NES_MASTER (a cooler variant) skews oranges/golds green. Pass
# `master=NES_MASTER_FCEUX` to any render fn to select it; the 2C02 has many accepted RGB tables.
NES_MASTER_FCEUX = [
    (124,124,124),(0,0,252),(0,0,188),(68,40,188),(148,0,132),(168,0,32),(168,16,0),(136,20,0),
    (80,48,0),(0,120,0),(0,104,0),(0,88,0),(0,64,88),(0,0,0),(0,0,0),(0,0,0),
    (188,188,188),(0,120,248),(0,88,248),(104,68,252),(216,0,204),(228,0,88),(248,56,0),(228,92,16),
    (172,124,0),(0,184,0),(0,168,0),(0,168,68),(0,136,136),(0,0,0),(0,0,0),(0,0,0),
    (248,248,248),(60,188,252),(104,136,252),(152,120,248),(248,120,248),(248,88,152),(248,120,88),(252,160,68),
    (248,184,0),(184,248,24),(88,216,84),(88,248,152),(0,232,216),(120,120,120),(0,0,0),(0,0,0),
    (252,252,252),(164,228,252),(184,184,248),(216,184,248),(248,184,248),(248,164,192),(240,208,176),(252,224,168),
    (248,216,120),(216,248,120),(152,248,176),(176,248,204),(156,252,240),(248,212,252),(0,0,0),(0,0,0),
]
GRAYSCALE16 = [0x0F, 0x00, 0x10, 0x30] * 4   # default 4-grey palette for atlases


def decode_tile(chr, idx, base=0):
    """2bpp CHR tile -> 8x8 grid of 0..3 colour indices (the NES plane interleave)."""
    o = base + idx * 16
    return [[((chr[o+y] >> b) & 1) | (((chr[o+y+8] >> b) & 1) << 1) for b in range(7, -1, -1)]
            for y in range(8)]


def attr_subpalette(attr, col, row, attr_cols=8):
    """NES attribute table -> which of the 4 sub-palettes a tile cell uses (2x2 metatile quadrants)."""
    byte = attr[(row // 4) * attr_cols + (col // 4)]
    shift = ((row % 4) // 2) * 4 + ((col % 4) // 2) * 2
    return (byte >> shift) & 0x03


def _rgb(idx):
    return NES_MASTER[idx & 0x3F]


def render_tiles(chr, nametable, palette16, *, cols, rows, attr=None, subpalette=None,
                 transparent_index=None, bg=(0, 0, 0), chr_base=0, scale=1, master=None):
    """Render an arbitrary cols x rows tile grid. `nametable` is a flat list of cols*rows tile indices
    (concatenate pages to render them as one).

    Sub-palette selection (which of palette16's four 4-colour groups a cell uses):
      `subpalette` (flat list of cols*rows ids 0-3) wins if given -- per-tile resolution, for layouts
        whose palette doesn't follow the 4x4 attribute grid (mid-cell shifts, stitched pages);
      else `attr` (a raw NES attribute table) is decoded per the hardware 2x2-quad rule;
      else sub-palette 0 everywhere.
    `transparent_index`: nametable cells equal to it are left as `bg` (for sparse layouts / text spaces)."""
    M = master or NES_MASTER
    img = Image.new('RGB', (cols * 8, rows * 8), bg)
    px = img.load()
    tiles = {}
    for row in range(rows):
        for col in range(cols):
            i = row * cols + col
            ti = nametable[i]
            if transparent_index is not None and ti == transparent_index:
                continue
            grid = tiles.get(ti) or tiles.setdefault(ti, decode_tile(chr, ti, chr_base))
            sub = subpalette[i] if subpalette is not None else \
                (attr_subpalette(attr, col, row, (cols + 3) // 4) if attr else 0)
            pal = palette16[sub * 4: sub * 4 + 4]
            x0, y0 = col * 8, row * 8
            for y in range(8):
                grow = grid[y]
                for x in range(8):
                    px[x0 + x, y0 + y] = M[pal[grow[x]] & 0x3F]
    return img.resize((cols * 8 * scale, rows * 8 * scale), Image.NEAREST) if scale != 1 else img


def render_sprites(chr, oam, palette16, base, *, chr_base=0, sprite_h=8, scale=1, master=None):
    """Overlay OAM sprites (4 bytes each: y, tile, attr, x) onto `base` (a PIL image). Colour 0 is
    transparent. `sprite_h`=8 (default) or 16 (8x16 mode: top tile = tile&0xFE, bottom = +1, order
    swapped under vertical flip). palette16 here is the 16-byte sprite palette region (4 sub-palettes)."""
    M = master or NES_MASTER
    base = base.copy(); px = base.load()
    for i in range(0, len(oam), 4):
        y, tile, attrb, x = oam[i], oam[i+1], oam[i+2], oam[i+3]
        if y >= 0xEF:
            continue
        pal = palette16[(attrb & 3) * 4:(attrb & 3) * 4 + 4]
        fx, fy = attrb & 0x40, attrb & 0x80
        if sprite_h == 16:
            parts = [((tile & 0xFE) + 1, 0), (tile & 0xFE, 8)] if fy else \
                    [(tile & 0xFE, 0), ((tile & 0xFE) + 1, 8)]
        else:
            parts = [(tile, 0)]
        for t, dy in parts:
            grid = decode_tile(chr, t, chr_base)
            for ty in range(8):
                for tx in range(8):
                    c = grid[7 - ty if fy else ty][7 - tx if fx else tx]
                    if c == 0:
                        continue
                    X, Y = x + tx, y + dy + ty
                    if 0 <= X < base.width and 0 <= Y < base.height:
                        px[X, Y] = M[pal[c] & 0x3F]
    return base.resize((base.width * scale, base.height * scale), Image.NEAREST) if scale != 1 else base


def render_chr_atlas(chr, *, count=256, cols=16, palette16=None, chr_base=0, scale=3, master=None):
    """Lay out the first `count` CHR tiles as a cols-wide grid -- the debug/atlas view."""
    palette16 = palette16 or GRAYSCALE16
    rows = (count + cols - 1) // cols
    nt = list(range(count)) + [0] * (rows * cols - count)
    return render_tiles(chr, nt, palette16, cols=cols, rows=rows, chr_base=chr_base, scale=scale, master=master)


if __name__ == "__main__":
    import sys
    # quick CLI: render a CHR atlas from a raw blob or an iNES file with CHR-ROM
    #   py nes_render.py atlas <file> [offset] [count] [out.png]
    if len(sys.argv) >= 3 and sys.argv[1] == "atlas":
        data = open(sys.argv[2], "rb").read()
        if data[:4] == b"NES\x1a":          # iNES: skip header (+ trainer if present)
            data = data[16 + (512 if data[6] & 4 else 0):]
        off = int(sys.argv[3], 0) if len(sys.argv) > 3 else 0
        count = int(sys.argv[4], 0) if len(sys.argv) > 4 else 256
        out = sys.argv[5] if len(sys.argv) > 5 else "atlas.png"
        render_chr_atlas(data[off:], count=count).save(out)
        print(f"wrote {out} ({count} tiles from +0x{off:X})")
    else:
        print(__doc__)
