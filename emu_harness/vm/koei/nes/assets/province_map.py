#!/usr/bin/env python3
"""
province_map.py - KOEI ROTK province-map asset layer (locate + decode terrain grid -> nes-render).

The province "map" is the small 3-row x 6-col terrain mosaic ROTK draws on a province's battle/map
subscreen. This module owns the KOEI-specific knowledge -- WHERE the per-province map data lives, HOW
the 3-bit terrain field per cell decodes, the row->data-row pattern, and HOW a terrain id expands into
a 4-wide x 2-tall metatile of CHR tile indices -- and delegates ALL pixel work to the generic
`nes-render`. Dependency direction: koei-nes -> nes-render, never the reverse.

GROUNDED in the ROTK decompile (convert-not-rebuild; addresses ARE the ROM's own operands):
  DATA SOURCE (no save needed):
    province_data_block  bank 9  $8FC4, stride 168 bytes/province (id 1..58 -> idx 0..57).
    load_current_province_data_block ($ECCA) copies idx*168 into RAM $7B95 via os_service_16(svc 9);
    svc N reads from bank N, so the static scenario source is bank 9 $8FC4 -- we read it directly.
    A cell's terrain is the 3-bit field (bits 5..3): terrain = (block[r*14 + c] & 0x3F) >> 3.
  GRID BUILD  (draw_province_map_grid_full $9E88, bank 12):
    3 grid rows x 6 cols; per grid-row the data-row offset (local8) is:
      row 0 -> 4 ; row 1 -> (scroll_col+col)&1 ? 4 : 5 ; row 2 -> 5 .
    cell = (block[(scroll_row + local8)*14 + col + scroll_col] & 0x3F) >> 3 ; if row>11 -> 6 (void).
  BLIT (same sub): 6 display rows x 6 cols; terrain = grid[(disp_row//2)*6 + col].
    terrain<6: 4 tile indices = tile_terrain_bitmaps $B574[terrain*16 + off .. +4] (bank 3),
      off = map_cell_tile_offset_lut $A811[((scroll_col&1)<<1 + disp_row)*6 + col]  (vals 0/4/8/12).
    terrain==6 (off-grid): a blank-tile row (the ROM writes tile 1). Output: 24 tiles wide x 6 tall.
  CHR: tile index T (0x64..0xBA) backs the terrain tiles; chr_battle_main_panel $B004 (bank 3), 87
    tiles, is uploaded to PPU $1640 (= $1000 + 0x64*16). So CHR(T) = bank3 $B004 + (T-0x64)*16.
  PALETTE: tile_attr_cmd_lut $FE47 (bank 15) = 4 BG sub-palettes, rows '3E xx xx 30'.
"""
import sys
from pathlib import Path

from emu_harness.machines.nes import ppu as NR   # the generic NES pixels
from emu_harness.vm.koei.nes.titles import game_cfg

# ---- per-title province-map config (ROTK) ----
ROTK = {
    "game": "ro3k",
    "data_bank": 9, "province_data_block": 0x8FC4, "data_stride": 168,
    "n_provinces": 58,
    "cell_stride": 14,            # the 14-wide row stride within the 168-byte block
    "grid_cols": 6, "grid_rows": 3, "disp_rows": 6,
    "void_terrain": 6,            # off-grid / sea filler terrain id
    "tile_bitmaps_bank": 3, "tile_terrain_bitmaps": 0xB574,   # 16 bytes/terrain = 4 quadrant-rows x 4 tiles
    "offset_lut_bank": 12, "map_cell_tile_offset_lut": 0xA811,  # 24 bytes (4 rows x 6 cols), vals 0/4/8/12
    "chr_bank": 3, "chr_panel": 0xB004, "chr_panel_tile0": 0x64, "chr_panel_ntiles": 87,
    "chr_ppu_base": 0x0,          # our 256-tile blob is indexed absolutely (tile T at byte T*16); the
                                  # PPU's $1000 base is folded into the blob layout, so no extra chr_base.
    "palette_bank": 15, "palette_lut": 0xFE47,  # tile_attr_cmd_lut: 16 bytes = 4 BG sub-palettes
    "blank_tile": 1,              # tile index the ROM writes for an off-grid (void) cell
    # ---- ECONOMY (starting stats, no scenario-load) ----
    # The persistent game state lives in NES SRAM ($6000-$7FFF), NOT a small ROM econ table.
    # On a fresh game, new_game_init ($8AB9) bulk-copies a DEFAULT SRAM IMAGE from bank-14 ROM
    #   $8004 -> SRAM $6001, length 0x1FFF  (syscall16_sram_wrap(14,$8004,$6001,$1FFF)).
    # The province record array $73A7 (stride 22) lives inside that image, so each province's
    # STARTING gold/rice/value/flood/pop/loyalty can be read straight from the ROM blob with no
    # save and no scenario select -- the econ is STATIC (these fields are not touched by the
    # scenario loader $8D5A, which only writes the faction/ownership fields +12/+13/+14).
    "econ_image_bank": 14, "econ_image_rom_addr": 0x8004,  # default-SRAM-image source in bank-14 ROM
    "econ_sram_base": 0x6001,                              # where that image lands in SRAM
    "province_record_array": 0x73A7, "province_record_stride": 22,
    # Field map = render_province_status_screen ($91xx bank 3) reading $73A7 record (bytecode-verified):
    #   +0 gold (word)  +2 rice (word)  +4 land-value (byte)  +5 flood (byte, shown 100-x)
    #   +6 population (word, shown x100)  +8 loyalty (byte)  +13 controlling-faction (byte)
}
PAL_MASTER = NR.NES_MASTER_FCEUX


def _prg(cfg):
    rom = cfg["_rom"].read_bytes()
    return rom[16:] if rom[:4] == b"NES\x1a" else rom


def _banked(prg, bank, addr):
    """One byte as if `bank` (a 16K PRG bank 0..15) were mapped at $8000-$BFFF; $C000+ = fixed bank 15."""
    if addr >= 0xC000:
        return prg[15 * 0x4000 + (addr - 0xC000)]
    return prg[(bank & 0x1F) * 0x4000 + (addr - 0x8000)]


# ---------------------------------------------------------------------------
# DATA: per-province 168-byte block (the source $7B95 is copied from) -- read straight from ROM.
# ---------------------------------------------------------------------------
def province_block(prg, province_id, cfg=ROTK):
    """The 168-byte province data block for province_id (1..58). idx = id-1."""
    idx = province_id - 1
    base = cfg["data_bank"] * 0x4000 + (cfg["province_data_block"] - 0x8000) + idx * cfg["data_stride"]
    return prg[base: base + cfg["data_stride"]]


# ---------------------------------------------------------------------------
# ECONOMY: per-province STARTING stats, read from the default SRAM image in bank-14 ROM.
# No save, no scenario-load -- the econ fields are static (scenario picks only ownership).
# ---------------------------------------------------------------------------
def province_econ(province_id, *, game="ro3k", cfg=ROTK, prg=None):
    """Starting economic stats for province_id (1..58), straight from ROM.

    Reads the persistent province record ($73A7 + (id-1)*22) out of the default SRAM image that
    new_game_init ($8AB9) copies from bank-14 ROM $8004. Field offsets + scales are exactly what
    the province status screen renders (bytecode-verified, bank 3 $91xx; format_string calls to
    str_gold_fmt/str_rice_fmt/str_value_fmt/str_flood_fmt/str_pop_fmt/str_loyalty_fmt).
    Cross-checked against the Yanzhou (prov 7) screenshot oracle: rice 20000, loyalty 55,
    pop 150000 reproduce exactly.

    Returns a dict of decoded, display-scaled values plus the raw stored bytes.
    """
    if prg is None:
        prg = _prg(game_cfg(game))
    rom0 = cfg["econ_image_bank"] * 0x4000 + (cfg["econ_image_rom_addr"] - 0x8000)
    rec_addr = cfg["province_record_array"] + (province_id - 1) * cfg["province_record_stride"]
    base = rom0 + (rec_addr - cfg["econ_sram_base"])
    rec = prg[base: base + cfg["province_record_stride"]]
    w = lambda o: rec[o] | (rec[o + 1] << 8)
    return {
        "province_id": province_id,
        "gold": w(0),                       # +0 word
        "rice": w(2),                       # +2 word
        "land_value": rec[4],               # +4 byte
        "flood": 100 - rec[5],              # +5 byte, displayed as 100-x (higher = drier/safer)
        "flood_stored": rec[5],
        "population": w(6) * 100,           # +6 word, displayed x100
        "population_stored": w(6),
        "loyalty": rec[8],                  # +8 byte
        "faction": rec[13],                 # +13 byte controlling-faction id (0 = unowned)
        "raw": bytes(rec),
    }


def terrain_grid(block, *, scroll_row=0, scroll_col=0, cfg=ROTK):
    """Decode the 3-row x 6-col terrain grid exactly as draw_province_map_grid_full ($9E88) does.
    Returns a flat list of 3*6 = 18 terrain ids (0..6)."""
    stride = cfg["cell_stride"]; cols = cfg["grid_cols"]
    grid = []
    for row in range(cfg["grid_rows"]):
        for col in range(cols):
            if row == 0:
                off = 4
            elif row == 1:
                off = 4 if ((scroll_col + col) & 1) else 5
            else:
                off = 5
            data_row = scroll_row + off
            if data_row > 11:
                grid.append(cfg["void_terrain"])
            else:
                v = block[data_row * stride + col + scroll_col]
                grid.append((v & 0x3F) >> 3)
    return grid


# ---------------------------------------------------------------------------
# FULL province map: the whole 168-byte block as a 12-row x 14-col terrain grid.
# The small $9E88 view only ever samples data-rows 4-5 (+scroll) through a 3x6 window; the FULL
# playfield is the entire block. scroll_province_map_render_up ($9A37) walks the block with the SAME
# `(row*14 + col) + 0x7B95` addressing and the SAME 3-bit decode -- it just covers all 12 rows. So the
# full grid = 12 rows x 14 cols (168 = 12*14), cell terrain = (block[r*14 + c] & 0x3F) >> 3.
# Verified across all 58 provinces: every byte is <= 0x3F (no bit6/7 set) so the decode is lossless,
# and terrain ids stay in 0..5 (6=void is only a window artifact, never stored).
# ---------------------------------------------------------------------------
FULL_ROWS, FULL_COLS = 12, 14    # 168-byte block = 12 data-rows x 14-col stride


def full_terrain_grid(block, cfg=ROTK):
    """Decode the ENTIRE 168-byte province block into a flat 12*14 = 168 list of terrain ids (0..5),
    cell = (block[r*stride + c] & 0x3F) >> 3 -- the same per-cell decode $9E88/$9A37 use."""
    stride = cfg["cell_stride"]
    grid = []
    for r in range(FULL_ROWS):
        for c in range(FULL_COLS):
            v = block[r * stride + c]
            grid.append((v & 0x3F) >> 3)
    return grid


def build_full_nametable(prg, full_grid, *, cfg=ROTK):
    """Expand the full 12x14 terrain grid into a (14*4)-wide x (12*2)-tall tile-index nametable using
    the SAME tile_terrain_bitmaps $B574 metatiles the windowed blit uses. Each terrain cell -> a 4-wide
    x 2-tall metatile; the per-quadrant tile offset comes from map_cell_tile_offset_lut $A811, indexed
    by (display-row-in-cell, col) exactly as $9E88 does (with scroll_col=0). void/blank cells (terrain
    6, never present in stored data) fall back to the blank tile."""
    tb_bank, tb = cfg["tile_bitmaps_bank"], cfg["tile_terrain_bitmaps"]
    lut_bank, lut = cfg["offset_lut_bank"], cfg["map_cell_tile_offset_lut"]
    cols6 = cfg["grid_cols"]          # the LUT is 4 rows x 6 cols (24 bytes); we index col % 6
    W = FULL_COLS * 4
    H = FULL_ROWS * 2
    nametable = [cfg["blank_tile"]] * (W * H)
    for gr in range(FULL_ROWS):
        for gc in range(FULL_COLS):
            terrain = full_grid[gr * FULL_COLS + gc]
            for sub in range(2):              # 2 display-rows per cell
                drow = gr * 2 + sub
                if terrain < cfg["void_terrain"]:
                    # offset LUT row = (scroll_col&1)<<1 + sub  (scroll_col=0 -> just sub), col = gc%6
                    lut_idx = (sub * cols6) + (gc % cols6)
                    off = _banked(prg, lut_bank, lut + lut_idx)
                    src = tb + (terrain << 4) + off
                    tiles = [_banked(prg, tb_bank, src + i) for i in range(4)]
                else:
                    tiles = [cfg["blank_tile"]] * 4
                for i in range(4):
                    nametable[drow * W + gc * 4 + i] = tiles[i]
    return nametable, W, H


def render_full_province_map(province_id, *, game="ro3k", cfg=ROTK, scale=8, subpalette=2,
                             out=None, prg=None):
    """Render the FULL 12x14 province terrain map (id 1..58) to a PNG via the generic renderer.
    This is the whole playfield (not the 3x6 scroll window). Returns (img, full_grid)."""
    if prg is None:
        prg = _prg(game_cfg(game))
    block = province_block(prg, province_id, cfg)
    grid = full_terrain_grid(block, cfg)
    nametable, W, H = build_full_nametable(prg, grid, cfg=cfg)
    pal = palette16(prg, cfg)
    sub = pal[subpalette * 4: subpalette * 4 + 4] + [0] * 12
    img = NR.render_tiles(assemble_chr(prg, cfg), nametable, sub, cols=W, rows=H,
                          chr_base=cfg["chr_ppu_base"], scale=scale, master=PAL_MASTER)
    if out:
        img.save(out)
    return img, grid


# ---------------------------------------------------------------------------
# KOEI metatile assembly: terrain grid -> a 24-wide x 6-tall nametable of CHR tile indices.
# ---------------------------------------------------------------------------
def build_nametable(prg, grid, *, scroll_col=0, cfg=ROTK):
    """Expand the 3x6 terrain grid into the ROM's 24-tile-wide x 6-tile-tall tile-index nametable,
    via tile_terrain_bitmaps $B574 (4 tiles/cell-row) selected by map_cell_tile_offset_lut $A811."""
    cols = cfg["grid_cols"]; disp_rows = cfg["disp_rows"]
    tb_bank, tb = cfg["tile_bitmaps_bank"], cfg["tile_terrain_bitmaps"]
    lut_bank, lut = cfg["offset_lut_bank"], cfg["map_cell_tile_offset_lut"]
    W = cols * 4
    nametable = [cfg["blank_tile"]] * (W * disp_rows)
    for drow in range(disp_rows):
        for col in range(cols):
            terrain = grid[(drow // 2) * cols + col]
            if terrain < cfg["void_terrain"]:
                lut_idx = (((scroll_col & 1) << 1) + drow) * cols + col
                off = _banked(prg, lut_bank, lut + lut_idx)
                src = tb + (terrain << 4) + off
                tiles = [_banked(prg, tb_bank, src + i) for i in range(4)]
            else:
                tiles = [cfg["blank_tile"]] * 4
            for i in range(4):
                nametable[drow * W + col * 4 + i] = tiles[i]
    return nametable, W, disp_rows


def assemble_chr(prg, cfg=ROTK):
    """A 256-tile CHR blob with the province-map panel tiles placed so a $B574 tile index T resolves
    directly: T*16 -> CHR byte. Panel tiles 0x64..0xBA come from chr_battle_main_panel $B004 (bank 3)."""
    chr = bytearray(256 * 16)
    t0, n = cfg["chr_panel_tile0"], cfg["chr_panel_ntiles"]
    base = cfg["chr_bank"] * 0x4000 + (cfg["chr_panel"] - 0x8000)
    for i in range(n):
        t = t0 + i
        chr[t * 16: t * 16 + 16] = prg[base + i * 16: base + i * 16 + 16]
    return bytes(chr)


def palette16(prg, cfg=ROTK):
    """4 BG sub-palettes (16 NES colour indices) from tile_attr_cmd_lut $FE47 (bank 15 fixed)."""
    base = 15 * 0x4000 + (cfg["palette_lut"] - 0xC000)
    return [prg[base + i] for i in range(16)]


# ---------------------------------------------------------------------------
# Top-level render.
# ---------------------------------------------------------------------------
def render_province_map(province_id, *, game="ro3k", cfg=ROTK, scale=8, subpalette=2, out=None):
    """Render a province's 3x6 terrain map (id 1..58) to a PNG via the generic renderer."""
    prg = _prg(game_cfg(game))
    block = province_block(prg, province_id, cfg)
    grid = terrain_grid(block, cfg=cfg)
    nametable, W, H = build_nametable(prg, grid, cfg=cfg)
    pal = palette16(prg, cfg)
    sub = pal[subpalette * 4: subpalette * 4 + 4] + [0] * 12
    img = NR.render_tiles(assemble_chr(prg, cfg), nametable, sub, cols=W, rows=H,
                          chr_base=cfg["chr_ppu_base"], scale=scale, master=PAL_MASTER)
    out = out or f"rotk-province-map-{province_id:02d}.png"
    img.save(out)
    print(f"wrote {out}  ({img.width}x{img.height}, province {province_id}, terrain grid {grid}, via nes-render)")
    return img, grid


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "econ":
        # `province_map.py econ [id|all]` -> dump starting econ stats from ROM (no save).
        prg = _prg(game_cfg("ro3k"))
        ids = range(1, 59) if (len(args) > 1 and args[1] == "all") else (
              [int(args[1])] if len(args) > 1 else [1, 7, 20])
        for pid in ids:
            e = province_econ(pid, prg=prg)
            print(f"prov {e['province_id']:2d}: gold={e['gold']:5d}  rice={e['rice']:6d}  "
                  f"value={e['land_value']:3d}  flood={e['flood']:3d}  "
                  f"pop={e['population']:7d}  loyalty={e['loyalty']:3d}  faction={e['faction']}")
    elif args and args[0] == "all3":
        for pid in (1, 7, 20):
            render_province_map(pid)
    else:
        pid = int(args[0]) if args else 1
        scale = int(args[args.index("--scale") + 1]) if "--scale" in args else 8
        sub = int(args[args.index("--sub") + 1]) if "--sub" in args else 2
        out = args[args.index("--out") + 1] if "--out" in args else None
        render_province_map(pid, scale=scale, subpalette=sub, out=out)
