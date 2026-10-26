#!/usr/bin/env python3
"""
animation.py - KOEI command/event ANIMATION asset layer (bank-14 animation-VM -> machines.nes.ppu).

Second KOEI-asset-layer specimen, ported from NA1's run-animation.py. It owns the KOEI-specific
ENGINE: the bank-14 animation bytecode VM. That VM reads an id, indexes a 5-byte descriptor table,
uploads a base CHR block to CHR-RAM, writes a palette, then runs a command stream that drives OAM
sprite frames + palette cycles + frame delays. We drive the ROM's OWN interpreter headless (na1dream),
intercept its graphics syscalls to keep a Python PPU model (CHR-RAM + palette + OAM), and snapshot a
frame at each present point. That part is the keep -- it's genuine per-title engine logic.

The eviction: run-animation carried its own `decode_tile` + a hardcoded `NES_PALETTE` and hand-rolled
the per-sprite pixel loop (flip/transparency/clip). All of that is GENERIC NES PPU work -- it now lives
in machines.nes.ppu. Per frame we hand machines.nes.ppu the (CHR, OAM, palette) and it returns the pixels via
`NR.render_sprites` over a backdrop built from the asset layer's `MASTER`. Zero own-core pixel decode here.

Dependency direction: vm.koei.nes.assets -> machines.nes.ppu (generic), never the reverse. ROM is resolved via
`game_cfg`; the SRAM trace (a captured mid-game state, lives in the na1 repo) is resolved relative to
that repo root -- no hardcoded absolute paths.
"""
import sys
from pathlib import Path

from emu_harness.machines.nes import ppu as NR   # the generic NES pixels
from emu_harness.vm.koei.nes.assets import MASTER
from emu_harness.vm.koei.nes.titles import game_cfg, make_vm
from PIL import Image

# ---- per-title animation-VM config ----
# Two CHR-upload styles are supported (KOEI moved the CHR uploader between titles):
#   "wrap":   a dedicated wrapper sub (NA1 $CF7C) is CALLed with (bank, src, dst, count) on the stack;
#             we intercept the CALL_abs_imm1 to that wrapper.  cfg["ppu_upload_wrap"].
#   "syscall": the upload is a kernel syscall task (ROTK task $01) dispatched through the native
#             syscall entry; we read its params off the VM data stack at the syscall call site.
#             cfg["chr_upload_task"] + cfg["chr_upload_args"] = (bank_off, src_off, dst_off, cnt_off).
#
# The kernel syscall dispatch lives at a per-title CPU address (NA1 $F226, JSR-style; ROTK $F8B7,
# BRK-style). NobunagaVM installs its _syscall_dispatch at $F226 by default; for a title whose
# dispatch is elsewhere we RELOCATE that same dispatcher (the syscall ABI -- task id at TOS, params
# at sp+2.. -- is byte-identical across the titles seen; ROTK's labels confirm "== NA1" for tasks
# $01/$03/$04/$13).  cfg["syscall_addr"] = where to (re)install it; None = leave NA1's $F226.

NA1 = {
    "game": "na1",
    "anim_desc": 0xAF80,       # bank-14 descriptor table, 5 bytes/entry: {bank, data_src(LE), stream(LE)}
    "anim_bank": 14,
    "addr_id": 0x7FCB,         # animation-id selector (written by ui_helper_e80c)
    "syscall_addr": None,      # keep NobunagaVM's default $F226 kernel dispatch
    "chr_style": "wrap",
    "ppu_upload_wrap": 0xCF7C, # ppu_upload_block_wrap(bank, src, dst_ppu, count) -- CHR uploader
    "present": (0xD73E, 0x8003),   # frame-present points (delay_loop, prompt_redraw)
    "oam_shadow": 0x0600,      # set_sprite ($03) target: [Y, tile, attr, X] * 64
    "pal_shadow": 0x0700,      # palette_write ($04) target: 32 entries
    "entry_pc": 0x80B2,        # $80AD body (skip prologue)
    "frame_off": 53,           # $80AD frame_off = -53 (sp = fp - 53)
    "n_scenes": 34,
    "sram": "traces/exp-fall-1560-start.dmp",   # captured mid-game state, relative to _root (repo/nobunaga)
}

# ROTK = NA1's animation engine RELOCATED bank 14 -> bank 10 (draw primitives byte-identical; the
# scene driver $80AD lightly evolved). Source: project ro3k labels + bank_10_vm.asm. The CHR uploader
# became a kernel syscall (task $01) and the kernel dispatch moved $F226 -> $F8B7. No SRAM trace is
# needed: $80AD reads the scene record (ROM bank 10 $8574) + script (ROM, via memcpy_banked) directly;
# we set the scene id ($7FB7) ourselves.
ROTK = {
    "game": "ro3k",
    "anim_desc": 0x8574,       # bank-10 cutscene_record_table, 5 bytes/entry {bank, script_ptr(LE), +2}
    "anim_bank": 10,
    "addr_id": 0x7FB7,         # cutscene_scene_id
    "syscall_addr": 0xF8B7,    # ROTK native syscall dispatch (BRK-based); relocate NA1 _syscall_dispatch here
    "chr_style": "syscall",
    "chr_upload_task": 0x01,   # ppu_upload_block syscall id
    "chr_upload_args": (2, 4, 6, 8),  # stack offsets (from sp) of bank, src, dst_ppu, count
    "present": (0x8003, 0xDEB4),   # cutscene_delay, delay_loop -- frame-present points
    "oam_shadow": 0x0600,      # set_sprite ($03) shadow (== NA1)
    "pal_shadow": 0x0700,      # palette_write ($04) shadow (== NA1)
    "entry_pc": 0x80B2,        # $80AD body (frame_off=-55)
    "frame_off": 55,
    "n_scenes": 24,
    "sram": None,              # no trace needed (reads records/scripts straight from ROM)
}

CONFIGS = {"na1": NA1, "ro3k": ROTK}


def _cfg_for(game, cfg):
    """Resolve the per-title config: explicit cfg wins, else look it up by game name."""
    if cfg is not None and cfg.get("game") == game:
        return cfg
    if game in CONFIGS:
        return CONFIGS[game]
    return cfg or NA1


def _descriptor(vm, cfg, aid):
    base = cfg["anim_desc"] + aid * 5
    b = [vm.read_banked(cfg["anim_bank"], (base + i) & 0xFFFF) for i in range(5)]
    return b[0], b[1] | (b[2] << 8), b[3] | (b[4] << 8)


def _install_syscall(vm, cfg):
    """Relocate NobunagaVM's kernel syscall dispatcher to this title's dispatch address, if it
    differs from the default $F226. The dispatcher logic (task id at TOS, params sp+2..) is shared;
    only the CPU entry address moves (NA1 JSR $F226 vs ROTK BRK $F8B7)."""
    addr = cfg.get("syscall_addr")
    if addr and addr != 0xF226:
        vm.host_call_stubs.pop(0xF226, None)
        vm.install_stub(addr, vm._syscall_dispatch)


def _new_vm(game, cfg, sram=None):
    """Build a retargeted VM for `game`, install its syscall dispatch, optionally load an SRAM trace."""
    vm, gcfg = make_vm(game)
    _install_syscall(vm, cfg)
    if cfg.get("sram"):
        sram_path = Path(sram) if sram else (gcfg["_root"] / cfg["sram"])
        vm.load_sram(str(sram_path))
    elif sram:
        vm.load_sram(str(Path(sram)))
    return vm, gcfg


def list_descriptors(game="na1", cfg=None):
    """Dump the animation descriptor / scene-record table (the `--list` view)."""
    cfg = _cfg_for(game, cfg)
    vm, _g = _new_vm(game, cfg)
    print("id  bank  data_src  stream")
    for aid in range(cfg["n_scenes"]):
        bank, src, stream = _descriptor(vm, cfg, aid)
        mark = "  <- Grow" if (game == "na1" and aid == 9) else ""
        print(f"{aid:2d}   {bank:2d}    ${src:04X}     ${stream:04X}{mark}")


def interpret_animation(aid, *, game="na1", cfg=None, sram=None, trace=False):
    """KOEI engine: drive the animation VM headless and capture per-frame PPU state.

    Returns (frames, descriptor) where frames is a list of (oam_bytes, pal_bytes, chr_bytes) -- the
    raw planes for each present point. NO pixel decode happens here; that's machines.nes.ppu's job. Works
    for any title with a CONFIGS block (NA1, ROTK); per-title differences (CHR-upload style, syscall
    dispatch address, present points, scene-id selector) are all in cfg.
    """
    cfg = _cfg_for(game, cfg)
    vm, _g = _new_vm(game, cfg, sram)
    sel = vm.mem.read(0x6F5F)
    vm.mem.write(cfg["addr_id"], aid)

    bank, src, stream = _descriptor(vm, cfg, aid)
    print(f"=== {game} animation id {aid}: bank {bank}, data ${src:04X}, stream ${stream:04X} (sel {sel}) ===")

    chr_ram = bytearray(0x2000)          # two pattern tables (CHR-RAM)
    frames = []                          # list of (oam_copy, pal_copy, chr_copy)
    state = {"uploads": 0, "present": 0}

    # The game enters an animation with every OAM slot hidden (Y=$F8; Mesen's OAM before Train, row
    # O3); headless, the shadow starts zeroed, and a zero slot is a visible sprite at (0,0).
    for i in range(64):
        vm.mem.write(cfg["oam_shadow"] + i * 4, 0xF8)
        vm.mem.write(cfg["oam_shadow"] + i * 4 + 3, 0xF8)

    vm.switch_bank(cfg["anim_bank"])
    fp = 0x05FF
    vm.vm_fp = fp
    vm.vm_sp = (fp - cfg["frame_off"]) & 0xFFFF
    vm.vm_pc = cfg["entry_pc"]
    vm.cpu.pc = vm.DISPATCHER_ADDR

    def snapshot():
        oam = bytes(vm.mem.read(cfg["oam_shadow"] + i) for i in range(0x100))
        pal = bytes(vm.mem.read(cfg["pal_shadow"] + i) for i in range(0x20))
        if frames and frames[-1][0] == oam and frames[-1][1] == pal:
            return                        # dedupe identical consecutive frames
        frames.append((oam, pal, bytes(chr_ram)))
        state["present"] += 1

    def do_upload(bank_, src_, dst_, cnt_):
        nbytes = (cnt_ & 0xFFFF) * 16     # count is in TILES (16 bytes/tile)
        for i in range(nbytes):
            chr_ram[(dst_ + i) & 0x1FFF] = vm.read_banked(bank_, (src_ + i) & 0xFFFF)
        state["uploads"] += 1
        if trace:
            print(f"  upload  bank={bank_} src=${src_:04X} -> PPU ${dst_:04X} x{cnt_} tiles")

    orig = vm.step_one_vm_op
    present = set(cfg["present"])
    chr_style = cfg["chr_style"]
    upload_wrap = cfg.get("ppu_upload_wrap")
    syscall_addr = cfg.get("syscall_addr") or 0xF226
    chr_task = cfg.get("chr_upload_task")
    chr_args = cfg.get("chr_upload_args")

    def hook():
        if vm.cpu.pc == vm.DISPATCHER_ADDR and vm.mem.read(vm.vm_pc) == 0xE9:
            tgt = vm.mem.read_word(vm.vm_pc + 1)
            sp = vm.vm_sp
            if chr_style == "wrap" and tgt == upload_wrap:
                # NA1: wrapper CALLed with (bank, src, dst, count) on the stack at sp+0..+6.
                do_upload(vm.mem.read_word(sp), vm.mem.read_word(sp+2),
                          vm.mem.read_word(sp+4), vm.mem.read_word(sp+6))
            elif chr_style == "syscall" and tgt == syscall_addr and vm.mem.read(sp) == chr_task:
                # ROTK: CHR upload is kernel syscall task $01; params at the cfg'd stack offsets.
                ba, sa, da, ca = chr_args
                do_upload(vm.mem.read(sp+ba), vm.mem.read_word(sp+sa),
                          vm.mem.read_word(sp+da), vm.mem.read_word(sp+ca))
            elif tgt in present:
                snapshot()
                if trace:
                    print(f"  present @ ${tgt:04X} frame#{state['present']}")
        return orig()
    vm.step_one_vm_op = hook

    try:
        vm.run_until_outermost_return(max_ops=500000)
    except Exception as e:
        print(f"  (stopped: {e})")
    snapshot()  # final

    print(f"uploads={state['uploads']}  frames={len(frames)}")
    return frames, (bank, src, stream)


def _sprite_palette16(pal):
    """The 32-byte PPU palette shadow -> a 16-entry sprite palette region (sub-palettes 4-7, $3F10+),
    in the layout ppu.render_sprites wants (4 sub-palettes x 4 NES indices)."""
    return [pal[0x10 + i] & 0x3F for i in range(16)]


def render_frames(frames, *, pt_base=0, sprite_h=8, scale=3, trace=False):
    """Generic: turn each captured (chr, oam, pal) frame into a PIL image -- delegated to machines.nes.ppu.

    machines.nes.ppu owns ALL pixel work: backdrop fill from NES_MASTER, then render_sprites does the
    per-sprite tile decode + palette + flip + transparency + clip. We only supply the planes and the
    sprite-window bbox bookkeeping (which is animation framing, not pixel decode).
    """
    if not frames:
        print("no frames captured")
        return [], []

    chr_base = pt_base                    # decode_tile's base is a byte offset, like pt_base
    imgs, frame_sprites = [], []
    sprite_bbox = None                   # bbox of the pixels actually drawn = the animation window
    for fi, (oam, pal, chr_snap) in enumerate(frames):
        active = [i for i in range(64) if oam[i*4] < 0xEF]
        frame_sprites.append(len(active))

        # The backdrop is $3F00, but the 32-byte upload's $3F10 write lands on it last (a hardware
        # mirror), so it is the shadow's $10; its $00 is never written by the animation (row O3).
        backdrop = Image.new("RGB", (256, 240), MASTER[pal[0x10] & 0x3F])
        pal16 = _sprite_palette16(pal)

        # Sprites go onto a transparent layer first, so the window is the pixels actually drawn
        # (na1's final crop); 8x16 mode, flips included, is render_sprites' own.
        layer = NR.render_sprites(chr_snap, bytes(oam[i*4:i*4+4][k] for i in active for k in range(4)),
                                  pal16, base=Image.new("RGBA", (256, 240), (0, 0, 0, 0)),
                                  chr_base=chr_base, sprite_h=sprite_h, master=MASTER)
        box = layer.getchannel("A").getbbox()
        if box:
            sprite_bbox = (list(box) if sprite_bbox is None
                           else [min(sprite_bbox[0], box[0]), min(sprite_bbox[1], box[1]),
                                 max(sprite_bbox[2], box[2]), max(sprite_bbox[3], box[3])])
        backdrop.paste(layer, (0, 0), layer)
        img = backdrop
        imgs.append(img)
        if trace:
            print(f"  frame {fi}: {len(active)} sprites")

    # Drop trailing clear/blank frames (single-scene cutscenes end with a sprite-clear).
    if imgs:
        peak = max(frame_sprites)
        while len(imgs) > 1 and frame_sprites[-1] < max(1, peak * 0.30):
            imgs.pop(); frame_sprites.pop()

    # Clip to the drawn sprite pixels, padded. NOT the composite's getbbox(): the backdrop
    # is palette entry 0 (often grey), so getbbox would return the whole 256x240.
    if sprite_bbox:
        pad = 8
        bbox = (max(0, sprite_bbox[0]-pad), max(0, sprite_bbox[1]-pad),
                min(256, sprite_bbox[2]+pad), min(240, sprite_bbox[3]+pad))
        imgs = [im.crop(bbox) for im in imgs]

    if scale != 1:
        w, h = imgs[0].size
        imgs = [im.resize((w*scale, h*scale), Image.NEAREST) for im in imgs]
    return imgs, frame_sprites


def render_animation(aid, *, game="na1", cfg=None, sram=None, pt=0, sprite_h=8, scale=3,
                     trace=False, out_prefix=None):
    """Full pipeline: interpret the animation VM, render its frames via machines.nes.ppu, write
    a GIF + a contact sheet. Returns the list of PIL frames."""
    frames, _desc = interpret_animation(aid, game=game, cfg=cfg, sram=sram, trace=trace)
    imgs, _fs = render_frames(frames, pt_base=pt * 0x1000, sprite_h=sprite_h, scale=scale, trace=trace)
    if not imgs:
        return []

    out_prefix = out_prefix or f"animation-{aid}"
    Path(out_prefix).parent.mkdir(parents=True, exist_ok=True)
    w, h = imgs[0].size
    imgs[0].save(f"{out_prefix}.gif", save_all=True, append_images=imgs[1:],
                 duration=120, loop=0, disposal=2)

    import math
    cols = min(8, len(imgs))
    rows = math.ceil(len(imgs) / cols)
    sheet = Image.new("RGB", (cols*(w+4), rows*(h+4)), (40, 40, 40))
    for i, im in enumerate(imgs):
        sheet.paste(im, ((i % cols)*(w+4)+2, (i // cols)*(h+4)+2))
    sheet.save(f"{out_prefix}-frames.png")
    print(f"wrote {out_prefix}.gif ({len(imgs)} frames, {w}x{h}) + -frames.png")
    return imgs


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Render a KOEI bank-14 animation via machines.nes.ppu.")
    ap.add_argument("id", nargs="?", type=int)
    ap.add_argument("--game", default="na1")
    ap.add_argument("--sram", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--pt", type=int, default=0, choices=(0, 1))
    ap.add_argument("--h", type=int, default=8, choices=(8, 16))
    ap.add_argument("--scale", type=int, default=3)
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        list_descriptors(a.game); sys.exit()
    if a.id is None:
        ap.error("need an animation id (or --list)")
    out = a.out or f"animation-{a.id}"
    render_animation(a.id, game=a.game, sram=a.sram, pt=a.pt, sprite_h=a.h,
                     scale=a.scale, trace=a.trace, out_prefix=out)
