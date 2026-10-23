#!/usr/bin/env python3
"""
strategic_map.py - KOEI illustrated STRATEGIC MAP asset layer (locate + assemble -> nes-render).

The richest KOEI-NES format: a metatile/section-assembly layer over generic 2bpp tiles. Ported from
NA1's render-strategic-map.py. It owns the KOEI-specific knowledge -- WHERE the map's tilemap / attr /
CHR / palette tables live, HOW a tilemap cell value indexes the (font|terrain) CHR-RAM, and HOW the 9
pannable SECTIONS stitch into one big panorama -- and delegates ALL pixel work (tile decode, attr ->
sub-palette, the big flexible cols x rows grid) to the generic `nes-render`. The eviction: NA1's
render-strategic-map carried its own `decode_tile` / `attr_palette` / `NES_PALETTE`; those are gone
here. We assemble a unified 256-tile CHR blob (so one flat index space covers font + terrain), a flat
nametable, and a per-cell attribute plane, then hand the whole map to NR.render_tiles in one call --
the flexible canvas (cols/rows arbitrary) is exactly the big-map case.

The KOEI engine format that STAYS here:
  - Tilemap : bank 4 $8D5C, 0x1C0 (=28x16) bytes per section, 9 sections (a pannable map).
  - Attrs   : bank 4 $9D1C, 0x20 (=32) bytes per section (BG sub-palette per 16x16 quad).
  - CHR     : the map tileset occupies PT1 $1510.. (= tile 81) from bank4 byte $83BC; tilemap cell V>=81
              is terrain at $83BC+(V-81)*16 (per the live PPU capture; tile 91 = $845C is just the
              anchor older notes named). Cells V<=80 are GAME-FONT glyphs (digits / region labels)
              living in PT1 $1000.. from bank0 $B2BA. We build a unified CHR so V indexes straight in.
  - Palette : the 4 BG sub-palettes are an authoritative PPU capture (sub-pals 1-3 are inherited
              ambient state, not a ROM table). Stored as NES colour indices -> nes-render does the RGB.
  - Stitch  : sections share fief RECORD lists ($9E3C); a global least-squares solve places every
              section on one virtual tilemap so the seam shear is distributed, not chained.

Config is per-title (NA1 here; a new KOEI title with an illustrated map adds its own block). Dependency
direction: koei-nes -> nes-render (generic), never the reverse.
"""
import sys
from pathlib import Path

from emu_harness.machines.nes import ppu as NR   # the generic NES pixels
from emu_harness.vm.koei.nes.assets import MASTER
from emu_harness.vm.koei.nes.titles import game_cfg
from emu_harness.vm.koei.nes.assets.text import char_to_tile as _font_char_to_tile     # reuse the KOEI font asset layer (no font dup)

# ---- per-title strategic-map config (NA1; from project_nobunaga_50fief_adjacency / *_visual_extraction) ----
NA1 = {
    "map_bank": 4,
    "tilemap": 0x8D5C,        # 0x1C0 (=28*16) bytes/section
    "attr":    0x9D1C,        # 0x20 (=32) bytes/section
    "records": 0x9E3C,        # 34 bytes/section: (col,row,fief_id) triples, 0xFF-terminated
    # bank4 terrain CHR: tile chr_tile0 <- chr_base, contiguous to tile 234. Grounded against the live
    # map-screen PPU capture (na1 traces/17fiefmapAPPU.dmp, PT1): tiles 81-90 are the 160 bytes before
    # the $845C (tile 91) anchor the docs name, and the tilemaps use them (cell 87 alone: 80x in sec 0).
    "chr_base": 0x83BC,
    "chr_tile0": 81,
    "sec_w": 28, "sec_h": 16,
    "n_sec": 9,
    "font_max_tile": 80,      # tilemap cell <= 80 is a FONT glyph; >= 81 is terrain
    "font_bank": 0, "font_addr": 0xB2BA,   # game font tiles 0..80 (PT1 $1000..)
    # the strategic map's 4 BG sub-palettes (authoritative PPU capture; see module docstring &
    # render-strategic-map.py rationale -- sub-pals 1-3 are inherited ambient state, no ROM table).
    "map_bg_palette": [
        [0x3E, 0x12, 0x29, 0x30],   # sub-pal0: backdrop / sea / land / white
        [0x3E, 0x02, 0x12, 0x21],   # sub-pal1: deep-sea depth gradient
        [0x3E, 0x18, 0x29, 0x30],   # sub-pal2: gold-brown earth / green / white (dominant land)
        [0x3E, 0x18, 0x01, 0x30],   # sub-pal3: gold-brown / dark-blue / white (mountain/coast)
    ],
}


def _prg(cfg):
    rom = cfg["_rom"].read_bytes()
    return rom[16:] if rom[:4] == b"NES\x1a" else rom


def _banked(prg, bank, addr):
    """One byte as if `bank` were mapped at $8000-$BFFF (the map tables/CHR all live in $8000+)."""
    return prg[(bank & 0x0F) * 0x4000 + (addr - 0x8000)]


def _palette16(cfg=NA1):
    """The 4 BG sub-palettes (4 NES colour indices each) flattened to a 16-entry palette16 for
    nes_render: sub-pal s, colour c -> palette16[s*4 + c]. nes-render does the index->RGB lookup."""
    pal = []
    for sub in cfg["map_bg_palette"]:
        pal += list(sub)
    return pal


# ---------------------------------------------------------------------------
# KOEI metatile/asset assembly -- the part that stays in the koei layer.
# ---------------------------------------------------------------------------
def assemble_chr(prg, cfg=NA1):
    """Build a unified 256-tile CHR blob keyed by tilemap cell value V, mirroring the game's CHR-RAM
    layout so a cell value indexes straight in (no per-cell font/terrain branch at decode time):
      V in 0..80  -> game-font glyph  (bank `font_bank` at font_addr + V*16)
      V >= chr_tile0(81) -> map terrain (bank `map_bank` at chr_base + (V-81)*16)
    Any tile between the two blocks is left blank (none, for NA1)."""
    fb, fa = cfg["font_bank"], cfg["font_addr"]
    mb, cb, t0 = cfg["map_bank"], cfg["chr_base"], cfg["chr_tile0"]
    fmax = cfg["font_max_tile"]
    chr = bytearray(256 * 16)
    for v in range(256):
        if v <= fmax:                                  # font glyph (2bpp, ink is any non-zero plane)
            src = (fb & 0x0F) * 0x4000 + (fa - 0x8000) + v * 16
        elif v >= t0:                                  # map terrain
            src = (mb & 0x0F) * 0x4000 + (cb - 0x8000) + (v - t0) * 16
        else:
            continue                                   # 81..90: gap, leave blank
        chr[v * 16: v * 16 + 16] = prg[src: src + 16]
    return bytes(chr)


def section_tilemap(prg, sec, cfg=NA1):
    """One section's 28x16 tilemap (flat list of cell values)."""
    base = cfg["tilemap"] + sec * 0x1C0
    return [_banked(prg, cfg["map_bank"], base + i) for i in range(0x1C0)]


def section_attr(prg, sec, cfg=NA1):
    """One section's 32-byte NES attribute table (BG sub-palette per 16x16 quad)."""
    base = cfg["attr"] + sec * 0x20
    return [_banked(prg, cfg["map_bank"], base + i) for i in range(0x20)]


def read_records(prg, sec, cfg=NA1):
    """{fief_id: (tx, ty)} in tilemap coords from the section's fief record list. Records are
    screen-space (col 0-27, row 4-19); ty = row - 4. The shared-fief overlap between sections is
    what lets us stitch the 9 pannable pages into one panorama."""
    base = cfg["records"] + sec * 34
    recs = {}
    for i in range(11):
        c = _banked(prg, cfg["map_bank"], base + i * 3)
        if c == 0xFF:
            break
        r = _banked(prg, cfg["map_bank"], base + i * 3 + 1)
        fid = _banked(prg, cfg["map_bank"], base + i * 3 + 2)
        recs[fid] = (c, r - 4)
    return recs


def _section_attr_plane(at, cfg=NA1):
    """Expand a section's 32-byte attr table into a per-tile sub-palette plane the size of the section,
    correcting the KOEI quirk: the section is blitted to screen cols 2-29 (MID attribute-cell), so the
    attribute byte for section-local col tx is decoded at screen col tx+2. Reading raw tx swaps
    sub-palettes on alternate quad-columns (lakes/mountains flip colour). Returns a flat list of
    sub-palette ids (sec_w*sec_h); nes-render then maps id -> the right 4 palette16 entries.

    Why expand here instead of handing nes-render the raw 32-byte attr table: nes_render.attr_subpalette
    assumes the standard PPU layout (cell origin aligned to the tile grid). The +2 screen-column shift is
    a KOEI placement detail, so the koei layer resolves it into an explicit per-tile plane."""
    w, h = cfg["sec_w"], cfg["sec_h"]
    plane = []
    for ty in range(h):
        for tx in range(w):
            sx = tx + 2                                   # section col 0 -> screen col 2
            cell = (ty // 4) * 8 + (sx // 4)
            b = at[cell] if cell < len(at) else 0
            quad = ((ty % 4) // 2) * 2 + ((sx % 4) // 2)  # 0 TL,1 TR,2 BL,3 BR
            plane.append((b >> (quad * 2)) & 3)
    return plane


# ---------------------------------------------------------------------------
# Global least-squares stitch (record-aligned section placement).
# Each shared fief between two sections is a constraint A_a - A_b = pos_b - pos_a per axis; solving all
# section offsets simultaneously (graph-Laplacian normal equations, gauge-fixed at the seed) distributes
# the residual page-seam shear across the map instead of chaining it from a seed.
# ---------------------------------------------------------------------------
def _gauss(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        d = M[col][col] or 1e-9
        for r in range(n):
            if r != col:
                f = M[r][col] / d
                for k in range(col, n + 1):
                    M[r][k] -= f * M[col][k]
    return [M[i][n] / (M[i][i] or 1e-9) for i in range(n)]


def layout_global(prg, sections, seed=0, cfg=NA1):
    """{section: (ax, ay)} in tilemap coords via a global least-squares record solve (gauge: seed@0)."""
    recs = {s: read_records(prg, s, cfg) for s in sections}
    idx = {s: i for i, s in enumerate(sections)}
    n = len(sections)

    def solve(axis):
        L = [[0.0] * n for _ in range(n)]
        c = [0.0] * n
        for a in sections:
            for b in sections:
                if a >= b:
                    continue
                for f in set(recs[a]) & set(recs[b]):
                    d = recs[b][f][axis] - recs[a][f][axis]
                    ia, ib = idx[a], idx[b]
                    L[ia][ia] += 1; L[ib][ib] += 1
                    L[ia][ib] -= 1; L[ib][ia] -= 1
                    c[ia] += d; c[ib] -= d
        si = idx[seed]
        L[si] = [1.0 if j == si else 0.0 for j in range(n)]
        c[si] = 0.0
        return _gauss(L, c)

    xs, ys = solve(0), solve(1)
    return {s: (round(xs[idx[s]]), round(ys[idx[s]])) for s in sections}


def _nn_fill(grid, x0, y0, x1, y1, default):
    """Fill any cell in the bbox missing from grid with its nearest set neighbour (notch/seam gaps)."""
    missing = [(x, y) for y in range(y0, y1) for x in range(x0, x1) if (x, y) not in grid]
    span = max(x1 - x0, y1 - y0)
    for (x, y) in missing:
        found = None
        for radius in range(1, span):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    if (x + dx, y + dy) in grid:
                        found = grid[(x + dx, y + dy)]
                        break
                if found:
                    break
            if found:
                break
        grid[(x, y)] = found if found else default


# ---------------------------------------------------------------------------
# Top-level: assemble the big nametable + attr plane, then delegate pixels to nes-render.
# ---------------------------------------------------------------------------
def render_section(prg, sec, *, cfg=NA1, scale=2):
    """One section, big -- assemble its 28x16 nametable + attr plane and render via the generic API."""
    chr = assemble_chr(prg, cfg)
    nametable = section_tilemap(prg, sec, cfg)
    attr_plane = _section_attr_plane(section_attr(prg, sec, cfg), cfg)
    return _render_with_plane(chr, nametable, attr_plane, cfg["sec_w"], cfg["sec_h"], scale, cfg)


def render_panorama(prg, sections, *, cfg=NA1, seed=0, scale=2):
    """Record-aligned stitch of the pannable sections into ONE big nametable, then render in a single
    nes_render.render_tiles call. KOEI knowledge (which tables, how sections align, palette) stays here;
    the flexible-canvas pixel work goes to nes-render. Returns (img, fief_px) where fief_px maps each
    1-based fief number to its label centre in OUTPUT (scaled) pixels."""
    chr = assemble_chr(prg, cfg)
    placed = layout_global(prg, sections, seed=seed, cfg=cfg)
    recs = {s: read_records(prg, s, cfg) for s in sections}
    w, h, fmax = cfg["sec_w"], cfg["sec_h"], cfg["font_max_tile"]

    # merge sections onto an unbounded virtual tilemap: terrain only (skip baked font/number tiles so
    # boundary fiefs don't double), first-writer-wins, carrying (tile_value, sub_palette).
    grid = {}
    for s in sorted(placed):                                # section order = overlap priority
        ax, ay = placed[s]
        tm = section_tilemap(prg, s, cfg)
        plane = _section_attr_plane(section_attr(prg, s, cfg), cfg)
        for ty in range(h):
            for tx in range(w):
                v = tm[ty * w + tx]
                if v <= fmax:                               # baked number -> leave a hole, redraw once
                    continue
                key = (tx + ax, ty + ay)
                if key not in grid:
                    grid[key] = (v, plane[ty * w + tx])
    xs = [x for x, y in grid]; ys = [y for x, y in grid]
    x0, x1, y0, y1 = min(xs), max(xs) + 1, min(ys), max(ys) + 1
    _nn_fill(grid, x0, y0, x1, y1, default=(grid[(xs[0], ys[0])]))

    W, H = x1 - x0, y1 - y0
    nametable = [0] * (W * H)
    attr_plane = [0] * (W * H)
    for (vx, vy), (v, p) in grid.items():
        i = (vy - y0) * W + (vx - x0)
        nametable[i] = v
        attr_plane[i] = p

    img = _render_with_plane(chr, nametable, attr_plane, W, H, scale, cfg)

    # one fief number per fief at its record position (priority = lowest placed section), drawn as
    # font glyphs over the rendered terrain.
    fief_pos = {}
    for s in sorted(placed):
        ax, ay = placed[s]
        for fid, (tx, ty) in recs[s].items():
            fief_pos.setdefault(fid, (tx + ax, ty + ay))
    fief_px = {}
    px = img.load()
    for fid, (vx, vy) in fief_pos.items():
        text = str(fid + 1)
        lx = min(max(0, (vx - x0) * 8 * scale), img.width - (len(text) * 8 + 2) * scale)
        ly = min(max(1 * scale, (vy - y0) * 8 * scale), img.height - 10 * scale)
        _draw_label(chr, px, img.width, img.height, lx, ly, text, scale, cfg)
        fief_px[fid + 1] = (lx + len(text) * 4 * scale, ly + 4 * scale)
    return img, fief_px


def _render_with_plane(chr, nametable, attr_plane, W, H, scale, cfg):
    """Render the assembled map through the generic renderer, honouring a PER-TILE sub-palette plane.

    *** This is the nes-render API gap (see module REPORT note). *** nes_render.render_tiles selects a
    cell's sub-palette from a raw NES ATTRIBUTE TABLE (1 byte / 4x4 tiles, 2 bits / 2x2 quad) via its
    internal attr_subpalette. The KOEI strategic map needs per-tile resolution: each section is blitted
    at screen col 2 (a +2 = one-quad column shift, mid attribute-cell), and stitched sections land at
    arbitrary virtual-tilemap offsets -- so the correct sub-palette is a per-tile fact that does NOT
    round-trip through the lower-resolution, grid-aligned attr-table API. With no per-tile-plane hook,
    we composite: render once PER sub-palette s (4 passes), each pass masking the nametable to the cells
    whose plane id == s, then keep only those cells. Correct, but 4x the pixel work of a single pass."""
    pal16 = _palette16(cfg)
    base = NR.render_tiles(chr, nametable, pal16[0:4] + [0] * 12, cols=W, rows=H, scale=scale, master=MASTER)
    bp = base.load()
    t = 8 * scale
    for s in range(1, 4):
        sub = pal16[s * 4: s * 4 + 4] + [0] * 12
        layer = NR.render_tiles(chr, nametable, sub, cols=W, rows=H, scale=scale, master=MASTER)
        lp = layer.load()
        for ty in range(H):
            for tx in range(W):
                if attr_plane[ty * W + tx] != s:
                    continue
                for yy in range(t):
                    for xx in range(t):
                        X, Y = tx * t + xx, ty * t + yy
                        bp[X, Y] = lp[X, Y]
    return base


def _draw_label(chr, px, W, H, lx, ly, text, scale, cfg):
    """Draw `text` in the game font (white on a black box) at output px (lx,ly). Glyphs are decoded
    from the SAME unified CHR block this module assembled (font tiles 0..80) via nes-render's decode_tile
    -- so no font is re-located here; ink is any non-zero (2bpp) pixel."""
    bw, bh = (len(text) * 8 + 2) * scale, 10 * scale
    for yy in range(-scale, bh - scale):
        for xx in range(-scale, bw - scale):
            x, y = lx + xx, ly + yy
            if 0 <= x < W and 0 <= y < H:
                px[x, y] = (0, 0, 0)
    for i, ch in enumerate(text):
        tile = _font_char_to_tile(ch)              # reuse text.py's char_classify (KOEI font layer)
        if tile is None or tile > cfg["font_max_tile"]:
            continue                               # only tiles 0..80 are in the unified map CHR
        grid = NR.decode_tile(chr, tile)               # 8x8 of 0..3; ink = non-zero
        for yy in range(8):
            for xx in range(8):
                if grid[yy][xx]:
                    for sy in range(scale):
                        for sx in range(scale):
                            x, y = lx + (i * 8 + xx) * scale + sx, ly + yy * scale + sy
                            if 0 <= x < W and 0 <= y < H:
                                px[x, y] = (255, 255, 255)


def panorama(game="na1", out="strategic_map.png", *, scenario=50, scale=2, cfg=NA1):
    """Deliverable: the stitched illustrated strategic-map panorama (the Map command's overview)."""
    prg = _prg(game_cfg(game))
    # 17-fief scenario uses sections {0,1}; 50-fief uses all 9 (see render-strategic-map rationale).
    sections = [0, 1] if scenario == 17 else list(range(cfg["n_sec"]))
    img, fief_px = render_panorama(prg, sections, cfg=cfg, scale=scale)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"wrote {out}  ({img.width}x{img.height}, scenario-{scenario}, {len(fief_px)} fiefs, via nes-render)")
    return img, fief_px


if __name__ == "__main__":
    args = sys.argv[1:]
    scenario = int(args[args.index("--scenario") + 1]) if "--scenario" in args else 50
    scale = int(args[args.index("--scale") + 1]) if "--scale" in args else 2
    out = args[args.index("--out") + 1] if "--out" in args else f"strategic_map_{scenario}.png"
    if "--section" in args:
        sec = int(args[args.index("--section") + 1])
        prg = _prg(game_cfg("na1"))
        img = render_section(prg, sec, scale=scale)
        img.save(out)
        print(f"wrote {out}  ({img.width}x{img.height}, section {sec}, via nes-render)")
    else:
        panorama(out=out, scenario=scenario, scale=scale)
