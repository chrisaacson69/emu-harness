#!/usr/bin/env python3
"""
portrait.py - KOEI daimyo/officer portrait asset layer (locate planes -> nes-render).

The first KOEI-asset-layer specimen, ported from NA1's render-portrait.py. It owns the KOEI-specific
knowledge -- WHERE the portrait planes live and HOW they're assembled -- and delegates all pixel work
(tile decode, palette, grid) to the generic `nes-render`. The eviction: NA1's render-portrait carried
its own `decode_tile` / `tile_img` / `NES_PALETTE`; those are gone here, replaced by nes-render.

Two KOEI portrait systems:
  COMPOSITE (RNG-generated daimyo): a base-5 "face code" picks a variant (0-4) of each of 4 feature
    blocks; the KOEI layer concatenates the chosen tiles into a synthetic CHR blob and renders it.
  PRESET (historical daimyo): a dedicated CHR block + a 6x6 tile-index layout map (rendered as a
    nametable) + a fixed 4-colour palette.

Config is per-title (NA1 here; a new title with portraits adds its own block). Dependency direction:
koei-nes -> nes-render (generic), never the reverse.
"""
import sys
from pathlib import Path

from emu_harness.machines.nes import ppu as NR   # the generic NES pixels
from emu_harness.vm.koei.nes.titles import game_cfg, make_vm

# ---- per-title portrait config (NA1; from project_nobunaga_daimyo_portraits) ----
NA1 = {
    "portrait_bank": 8,
    # COMPOSITE feature blocks: (name, chr_base, stride_bytes, map_base, tile_base, tiles_per_block).
    # Variant v uploads CHR chr_base+v*stride (portrait_bank) to virtual tiles tile_base.. and lays them
    # out by the n-byte tile-index slice map_base+v*n (map_bank); the 4 slices concatenate to the 6x6 face.
    "features": [("brow", 0xA604, 192, 0xAE34, 0x5B, 12), ("eyes", 0xA9C4, 96, 0xAE70, 0x67, 6),
                 ("nose", 0xABA4, 96, 0xAE8E, 0x6D, 6), ("mouth", 0xAD84, 192, 0xAEAC, 0x73, 12)],
    "map_bank": 9,
    "variants": 5,
    # PRESET (historical daimyo): a DEDICATED hand-drawn CHR block selected per mapid via a descriptor
    # table, laid out by a per-portrait 6x6 tile-index map.
    #   descriptor table: bank 8 $BBD0, 4 bytes/entry {chr_bank, tile_count, src_ptr_LE}, indexed by mapid
    #   layout map:       bank 8 $B144 + mapid*36 (6x6 tile indices); cell v -> CHR tile (v - 0x5B)
    #   palette:          4 NES indices at $F7CC (bank 15) = black / cream / gold / white
    # mapid: 50-fief daimyo idx == mapid (identity). Oda Nobunaga = descriptor[24].
    "preset_desc": 0xBBD0, "preset_desc_bank": 8,
    "preset_layout": 0xB144, "preset_tile_base": 0x5B,
    "palette4": [0x3E, 0x38, 0x28, 0x30],     # TRUE ROM indices $F7CC (black/cream/gold/white)
}
# Portraits render through nes-render's WARM master (NES_MASTER_FCEUX) so the ROM's real 2C02 indices
# land warm cream/gold (matching the in-game look + the original NA1 tool), instead of the cool default.
PAL_MASTER = NR.NES_MASTER_FCEUX
GRAY4 = [0x0F, 0x00, 0x10, 0x30]              # the composite sheet's greyscale ramp

# ---- ROTK officer portrait config (a THIRD KOEI portrait variant; from ro3k bank_15.c) ----
# ROTK's officer portrait is a COMPOSITE like NA1's, but richer: a packed 5-byte record at
# officer_panel_record $AFD4 (bank 7), officer_idx*5 = [group, v1, v2, v3, v4], drives the assembly
# done by load_and_draw_officer_record_panel ($E7F5). FOUR variant blocks (all bank 7) overlay the
# per-officer features into CHR-RAM at FIXED tile slots (the portrait branch, roster_page_index=0):
#   blockA: ((group*5+v1)*0x180 + $8004), 24 tiles -> PPU $1640 (tiles 0x64-0x7B)
#   blockB: ((group*5+v2)*48   + $9E04),  3 tiles -> PPU $17C0 (tiles 0x7C-0x7E)
#   blockC: ((group*5+v3)*48   + $A1C4),  3 tiles -> PPU $17F0 (tiles 0x7F-0x81)
#   blockD: ((group*5+v4)*96   + $A584),  6 tiles -> PPU $1820 (tiles 0x82-0x87)
# A 36-byte (6x6) tile-index LAYOUT comes from ((group*5+v1)*36 + $AD04) -- but its center 12 cells are
# 0-placeholders that $E7F5 fixes up at run time into the feature-tile indices 0x7C-0x87. So we RUN the
# ROM's own $E7F5 headless ONLY for that fixed-up layout (the part that's painful to re-derive), and
# assemble the CHR-RAM DETERMINISTICALLY from the four bytecode-verified block formulas above (the older
# arg-hook capture mis-read the VM frame's marshalled stack and bled the panel's frame tiles into the
# face -- that was the bug). Layout tile T -> CHR byte chr_base + T*16; chr_base $1000 makes tile 0x64
# land at $1640, exactly blockA's dst. Palette = first 4 of tile_palette_attr_lut $FE2A. idx 0 = Cao Cao.
ROTK = {
    "game": "ro3k",
    "panel_sub": 0xE7FA,        # body of load_and_draw_officer_record_panel ($E7F5)
    # feature blocks: (record_field_index, src_base_cpu, stride, dst_ppu, ntiles); src = (group*5+vN)*stride+base
    "feature_blocks": [(1, 0x8004, 0x180, 0x1640, 24), (2, 0x9E04, 48, 0x17C0, 3),
                       (3, 0xA1C4, 48, 0x17F0, 3), (4, 0xA584, 96, 0x1820, 6)],
    "feature_bank": 7,
    "upload_wrap": 0xC9C3,      # ppu_upload_block_wrap -- the feature-block uploader ($E7F5 calls it)
    "syscall_addr": 0xF8B7,     # ROTK kernel syscall dispatch (relocate NA1 _syscall_dispatch here)
    "roster_page_index": 0x7FB1,    # 0 -> portrait (not roster) branch, contiguous block placement
    "out_buf": 0x0400,          # scratch RAM for the 36-byte layout map $E7F5 writes
    "record_bank": 7,
    "officer_record": 0xAFD4,   # officer_panel_record (stride 5)
    "name_table": 0xA304, "name_bank": 14, "name_stride": 13,   # officer_name_table (ASCII)
    "palette_lut": 0xFE2A,      # tile_palette_attr_lut[0:4] = portrait sub-palette
    "chr_base": 0x1000,         # layout tile T (table-1-relative) -> CHR byte (T+256)*16
}


def _prg(cfg):
    rom = cfg["_rom"].read_bytes()
    return rom[16:] if rom[:4] == b"NES\x1a" else rom


def _pal16(four):
    """4 NES colour indices -> a 16-entry palette16 (sub-palette 0) for nes_render.render_tiles."""
    return list(four) + [0] * 12


def assemble_composite_chr(prg, variants, cfg=NA1):
    """KOEI-specific: concatenate the chosen variant tiles of each feature block into one CHR blob.
    The blocks' tile bases are contiguous, so blob tile i is virtual tile features[0].tile_base + i."""
    bank = cfg["portrait_bank"]; blob = bytearray()
    for (name, base, stride, _map, _tbase, ntiles), v in zip(cfg["features"], variants):
        off = bank * 0x4000 + ((base + v * stride) & 0x3FFF)
        blob += prg[off: off + ntiles * 16]
    return bytes(blob)                         # 12+6+6+12 = 36 tiles


def composite_layout(prg, variants, cfg=NA1):
    """KOEI-specific: the face's 36-cell tile map, each feature's variant slice from map_bank in turn."""
    bank = cfg["map_bank"]; cells = bytearray()
    for (name, _base, _stride, mbase, _tbase, ntiles), v in zip(cfg["features"], variants):
        off = bank * 0x4000 + ((mbase + v * ntiles) & 0x3FFF)
        cells += prg[off: off + ntiles]
    return bytes(cells)


def render_composite_face(prg, variants, *, cfg=NA1, scale=6, gray=True):
    """Assemble a base-5 composite face and render it through the GENERIC renderer: the feature tiles
    as CHR, the concatenated map slices as a 6x6 nametable (cells outside the 36 tiles = colour 0)."""
    chr = assemble_composite_chr(prg, variants, cfg)
    t0 = cfg["features"][0][4]
    nametable = [(c - t0) & 0xFF for c in composite_layout(prg, variants, cfg)]
    pal = GRAY4 if gray else cfg["palette4"]
    return NR.render_tiles(chr, nametable, _pal16(pal), cols=6, rows=6, scale=scale, master=PAL_MASTER)


def preset_descriptor(prg, mapid, cfg=NA1):
    """KOEI-specific: locate a historical daimyo's dedicated portrait CHR via the descriptor table.
    bank 8 $BBD0 + mapid*4 = {chr_bank, tile_count, src_ptr_LE}. Returns (chr_bank, count, chr_bytes)."""
    o = cfg["preset_desc_bank"] * 0x4000 + ((cfg["preset_desc"] + mapid * 4) & 0x3FFF)
    cbank, cnt, lo, hi = prg[o:o + 4]
    src = cbank * 0x4000 + (((lo | (hi << 8)) & 0x3FFF))
    return cbank, cnt, prg[src: src + cnt * 16]


def render_preset_portrait(prg, mapid, portrait_chr=None, *, cfg=NA1, scale=6):
    """Render a historical portrait: its 6x6 layout map (a nametable) over its dedicated CHR block.
    If `portrait_chr` is None the dedicated CHR is sourced via the descriptor table (the wired path)."""
    if portrait_chr is None:
        _b, _cnt, portrait_chr = preset_descriptor(prg, mapid, cfg)
    base = cfg["portrait_bank"] * 0x4000 + ((cfg["preset_layout"] + mapid * 36) & 0x3FFF)
    # Layout cell v -> CHR tile (v - 0x5B); the filler/background tile 0x5B maps to -1 (out of range) and
    # is rendered as palette colour 0 (the portrait background) -- exactly like the ROM's blit.
    nametable = [(b - cfg["preset_tile_base"]) & 0xFF for b in prg[base: base + 36]]
    return NR.render_tiles(portrait_chr, nametable, _pal16(cfg["palette4"]),
                           cols=6, rows=6, scale=scale, master=PAL_MASTER)


def render_na1_daimyo(mapid=24, *, game="na1", cfg=NA1, scale=6, out=None):
    """Render a clean historical NA1 daimyo portrait via the PRESET path (descriptor -> CHR -> layout).
    mapid 24 = Oda Nobunaga (50-fief daimyo idx == mapid)."""
    prg = _prg(game_cfg(game))
    img = render_preset_portrait(prg, mapid, cfg=cfg, scale=scale)
    out = out or f"na1-portrait-mapid{mapid}.png"
    img.save(out)
    print(f"wrote {out}  (NA1 daimyo mapid {mapid}, preset CHR via descriptor[{mapid}], via nes-render)")
    return img


def faces_sheet(game="na1", out="portrait_faces.png", cfg=NA1):
    """Deliverable: a row of composite faces across several base-5 combos (the old `faces` command)."""
    from PIL import Image
    prg = _prg(game_cfg(game))
    combos = [(0,0,0,0),(1,1,1,1),(2,2,2,2),(3,3,3,3),(4,4,4,4),(0,1,2,3),(4,3,2,1),(2,0,4,1)]
    imgs = [render_composite_face(prg, c, cfg=cfg) for c in combos]
    w, h = imgs[0].size
    sheet = Image.new("RGB", (len(imgs) * (w + 8) + 8, h + 16), (40, 40, 60))
    for i, im in enumerate(imgs):
        sheet.paste(im, (8 + i * (w + 8), 8))
    sheet.save(out)
    print(f"wrote {out}  ({len(combos)} composite faces, via nes-render)")


# ===================== ROTK officer portrait (composite via the ROM's own assembler) =====================

def rotk_officer_name(vm, idx, cfg=ROTK):
    """Officer name (ASCII) at officer_name_table; idx*stride. Cao Cao = idx 0."""
    base = cfg["name_table"] + idx * cfg["name_stride"]
    bs = bytes(vm.read_banked(cfg["name_bank"], base + i) for i in range(12))
    return bs.split(b"\x00", 1)[0].decode("latin1", "replace").strip()


def rotk_assemble_chr(vm, record, cfg=ROTK):
    """Assemble the officer's CHR-RAM DETERMINISTICALLY from the four bytecode-verified feature-block
    formulas (convert-not-rebuild: the addresses ARE the ROM's $E7F5 operands). `record` = the 5-byte
    [group,v1,v2,v3,v4]. Each block N: src = (group*5 + record[fieldN])*stride + base; copied to its
    fixed PPU dst. Returns a 0x2000 CHR-RAM with the face tiles at $1640-$1880."""
    group = record[0]; fb = cfg["feature_bank"]
    chr_ram = bytearray(0x2000)
    for field, base, stride, dst, ntiles in cfg["feature_blocks"]:
        src = ((group * 5 + record[field]) * stride + base) & 0xFFFF
        for i in range(ntiles * 16):
            chr_ram[(dst + i) & 0x1FFF] = vm.read_banked(fb, (src + i) & 0xFFFF)
    return bytes(chr_ram)


def rotk_assemble_portrait(idx, *, game="ro3k", cfg=ROTK):
    """Assemble a ROTK officer portrait: CHR-RAM deterministically from the feature-block formulas, and
    the fixed-up 6x6 layout by running the ROM's OWN $E7F5 (convert-not-rebuild; its center cells are
    runtime placeholders not in any static table). Returns (chr_ram, layout36, pal4, name)."""
    vm, _g = make_vm(game)
    # Relocate NA1's kernel syscall dispatcher to ROTK's $F8B7 (memcpy_banked etc. used by $E7F5).
    sysaddr = cfg["syscall_addr"]
    if sysaddr and sysaddr != 0xF226:
        vm.host_call_stubs.pop(0xF226, None)
        vm.install_stub(sysaddr, vm._syscall_dispatch)
    vm.switch_bank(cfg["record_bank"])
    vm.mem.write(cfg["roster_page_index"], 0)         # take the portrait (not roster) branch

    # 1) CHR-RAM: deterministic, from the bytecode-verified block formulas (no fragile arg-hook).
    record = [vm.read_banked(cfg["record_bank"], cfg["officer_record"] + idx * 5 + i) for i in range(5)]
    chr_ram = rotk_assemble_chr(vm, record, cfg)

    # 2) Layout: run $E7F5 headless purely for the fixed-up 36-byte map it writes to `out`.
    out = cfg["out_buf"]
    fp = 0x05FF
    vm.vm_fp = fp; vm.vm_sp = fp
    vm.mem.write_word(fp + 0x0B, idx)                 # arg1 = officer index
    vm.mem.write_word(fp + 0x0D, out)                 # arg2 = layout-map output buffer
    vm.vm_pc = cfg["panel_sub"]
    vm.cpu.pc = vm.DISPATCHER_ADDR
    try:
        vm.run_until_outermost_return(max_ops=200000)
    except Exception as e:
        print(f"  (stopped: {e})")

    layout = [vm.mem.read(out + i) for i in range(36)]
    pal4 = [vm.read_banked(15, cfg["palette_lut"] + i) for i in range(4)]
    return chr_ram, layout, pal4, rotk_officer_name(vm, idx, cfg)


def render_rotk_portrait(idx=0, *, game="ro3k", cfg=ROTK, scale=12, out=None):
    """Render a ROTK officer portrait (idx 0 = Cao Cao) by running the ROM's own assembler and laying
    the captured 6x6 tile map over the captured CHR-RAM through the GENERIC renderer."""
    chr_ram, layout, pal4, name = rotk_assemble_portrait(idx, game=game, cfg=cfg)
    img = NR.render_tiles(chr_ram, layout, _pal16(pal4), cols=6, rows=6,
                          chr_base=cfg["chr_base"], scale=scale, master=PAL_MASTER)
    out = out or f"rotk-portrait-{name.replace(' ', '-').lower() or idx}.png"
    img.save(out)
    print(f"wrote {out}  (ROTK officer {idx} '{name}', CHR from feature-block formulas + "
          f"layout via $E7F5, via nes-render)")
    return img


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "faces"
    if cmd == "faces":
        faces_sheet(out=sys.argv[2] if len(sys.argv) > 2 else "portrait_faces.png")
    elif cmd == "rotk":
        idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        render_rotk_portrait(idx, out=sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "na1":
        mapid = int(sys.argv[2]) if len(sys.argv) > 2 else 24
        render_na1_daimyo(mapid, out=sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        print(__doc__)
