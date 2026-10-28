#!/usr/bin/env python3
"""
titles.py - the KOEI NES title profiles (games.toml) and the per-title VM drivers (disassemble,
decompile, emulate). Was koei-nes `tools/koei_vm.py`; koei-nes keeps that name as an alias of this
module (emu-harness MIGRATION.md row F).

The KOEI Famicom VM is ~217/219 opcodes byte-identical across titles, and these tools step the ROM's
OWN interpreter -- so a new title is a few retarget ADDRESSES, not a code fork. All per-title values
live in games.toml (next to this file); this driver reads them and drives the VM core
(emu_harness.vm.koei) generically.

  py -3.14 koei_vm.py disasm  <game> <bank> [out.asm]   # render a bank's bytecode VM-asm
  py -3.14 koei_vm.py emulate <game> <0xSUB> [args...] [--bank N] [--sram F]  # run a bytecode sub, print regL
  py -3.14 koei_vm.py games                             # list configured titles
  (or: py -3.14 -m emu_harness.vm.koei.nes.titles ...)

`<game>` is a block name in games.toml (na1, ro3k, ...). A title's `repo` is a folder next to
emu-harness (the sibling convention).
"""
import sys, re
from pathlib import Path

from emu_harness.machines.nes.memory import Cart, Windows

HERE = Path(__file__).resolve()
SIBLINGS = HERE.parents[5]                         # nes -> koei -> vm -> emu_harness -> emu-harness -> the sibling root
GAMES_TOML = HERE.parent / "games.toml"


def load_games():
    """games.toml via tomllib (py 3.11+). It replaced a line regex that knew only [title] headers:
    a [title.native_floor] table made it pour those keys into the previous title. Same values for
    every existing title (checked 2026-10-22, emu-harness MIGRATION.md row D)."""
    import tomllib
    return tomllib.loads(GAMES_TOML.read_text(encoding="utf-8"))


def game_cfg(name):
    games = load_games()
    if name not in games:
        sys.exit(f"unknown game '{name}'; known: {', '.join(games)}")
    g = games[name]
    g["_root"] = SIBLINGS / g["repo"]
    g["_rom"] = g["_root"] / g["rom"]
    g["_labels"] = g["_root"] / g.get("labels", "mesen-labels.toml")
    return g


def bank_base_for(cfg, bank):
    """CPU base of a bytecode bank's window. MMC5 titles can map "shared library" banks at $8000
    ($5114) and "app-logic" banks at $A000 ($5115); the per-title `banks_at_8000` list overrides
    the default `bank_base`. MMC1 titles ignore this (bank_base default $8000, no overrides).
    The profile's answer only: the cart's fixed banks are not applied, as this function never did
    (emu-harness MIGRATION.md row N); `windows(cfg)` is the full model."""
    return windows(cfg).base(bank, profile_only=True)


_WINDOWS = {}

def windows(cfg):
    """The title's bank windows (emu_harness Windows): the cart's fixed banks + the profile's lists.
    The one place a KOEI tool asks where a bank runs."""
    key = (str(cfg["_rom"]), cfg.get("bank_base"),
           *(tuple(cfg.get(k, ())) for k in ("banks_at_8000", "banks_at_c000", "banks_at_e000")))
    if key not in _WINDOWS:
        _WINDOWS[key] = Windows.from_profile(Cart.from_ines(cfg["_rom"]), cfg)
    return _WINDOWS[key]


def build_syscall_names(cfg, labels):
    """Game-aware syscall id -> name, resolved through the title's `syscall_table` + the handler
    labels (TOML-driven, NOT hardcoded). Decodes the table from the ROM per `syscall_kind`
    (jsr3 = 3-byte JMP entries; brk2/fakebrk_priv = raw 2-byte addresses), maps each id to its
    handler address, and looks the address up in the title's labels. Empty -> caller keeps default."""
    table = cfg.get("syscall_table", 0)
    if not table:
        return {}
    kind = cfg.get("syscall_kind", "")
    rom = Path(cfg["_rom"]).read_bytes()
    fixed = 0x10 + (rom[4] - 1) * 0x4000        # last 16K of PRG = the $C000-$FFFF fixed bank
    def rd(cpu):                                # (was hardcoded 15*0x4000 = 256K-only; wrong for the 512K UW cart; same fix as emu-harness machines.nes.memory.fixed_bank)
        return rom[fixed + (cpu - 0xC000)]
    stride, off = (3, 1) if kind == "jsr3" else (2, 0)   # jsr3: addr after the 4C; else raw 2-byte
    out = {}
    for i in range(64):
        e = table + i * stride
        if e + stride > 0x10000:
            break
        a = rd(e + off) | (rd(e + off + 1) << 8)
        if not (0xC000 <= a <= 0xFFFF):
            break
        nm = labels.get(a)
        if nm:
            out[i] = nm[0] if isinstance(nm, tuple) else nm
    return out


def bank_scoped_labels(cfg, bank):
    """addr -> (name, comment) from [ram], [prg.data], [prg.bank15], [prg.bank<bank>] -- the
    bank-unambiguous label view every per-title tool must use ($8000-$BFFF collides across banks)."""
    txt = cfg["_labels"].read_text(encoding="utf-8")
    # fixed-region label scope: MMC1 titles = bank 15; MMC5 (na2) = bank 31 ($E000-$FFFF) via fixed_section,
    # so the legacy "prg.bank15" name is freed for na2's ACTUAL bank 15 (ending/battle-resolution bytecode).
    want = {"ram", "prg.data", cfg.get("fixed_section", "prg.bank15"), f"prg.bank{bank}"}
    for cb in cfg.get("banks_at_c000", []):            # semi-fixed lower bank ($C000-$DFFF, always mapped):
        want.add(f"prg.bank{cb}")                      # its OS/record library is called directly by every bank
    for p in cfg.get(f"coresident_{bank}", []):        # co-resident partner banks (direct CALL_abs targets)
        want.add(f"prg.bank{p}")
    pat = re.compile(r'"0x([0-9A-Fa-f]+)"\s*=\s*\{\s*name\s*=\s*"([^"]+)"(?:,\s*comment\s*=\s*"([^"]*)")?')
    sec_re = re.compile(r'^\s*\[([^\]]+)\]')
    labs, sec = {}, ""
    for ln in txt.splitlines():
        ms = sec_re.match(ln)
        if ms:
            sec = ms.group(1); continue
        if sec not in want:
            continue
        m = pat.match(ln.strip())
        if m:
            labs[int(m.group(1), 16)] = (m.group(2), m.group(3) or "")
    return labs


def build_bank_call_map(cfg):
    """{(bank, addr) -> name} from every [prg.bankN] section. Lets the decompiler resolve the
    cross-bank trampoline call_in_bank_dcd1(window, bankval, addr, …) to the real target name
    (target bank = bankval & 31). Without this, cross-bank calls render as raw (2, 224, 0xA812)."""
    txt = cfg["_labels"].read_text(encoding="utf-8")
    pat = re.compile(r'"0x([0-9A-Fa-f]+)"\s*=\s*\{\s*name\s*=\s*"([^"]+)"')
    sec_re = re.compile(r'^\s*\[prg\.bank(\d+)\]')
    out, cur = {}, None
    for ln in txt.splitlines():
        ms = sec_re.match(ln)
        if ms:
            cur = int(ms.group(1)); continue
        if ln.strip().startswith("["):
            cur = None; continue
        if cur is None:
            continue
        m = pat.match(ln.strip())
        if m:
            out[(cur, int(m.group(1), 16))] = m.group(2)
    return out


def _import_core():
    """Kept for callers of the old koei_vm API: the core is part of this package now."""


def bank_view(name, bank):
    """One bytecode bank of a title, set up the way every NES pipeline step reads it: the title's
    vm_entry stub pattern, bank size and window (the full model: the cart's fixed banks at
    $C000/$E000; bank_base_for skips those, so UW's bank 63 rendered at $A000, F2) on vm_disasm, and
    a VM runner with the title's ROM + labels and that bank switched in.
    -> (vm, vm_disasm module, cfg, base, size, stubs)"""
    cfg = game_cfg(name)
    import emu_harness.vm.koei.nes.vm_disasm as VD
    from emu_harness.vm.koei.nes.nobunaga_vm import NobunagaVM
    VD.VM_ENTRY_STUB = bytes(int(b, 16) for b in cfg["vm_entry_stub"].split())
    VD.ROM_NAME = cfg["rom"]                            # per-title banner (was hardcoded NA1)
    VD.BANK_SIZE = cfg.get("bank_size", 0x4000)        # 0x2000 for MMC5 8K-bank titles (na2)
    base = windows(cfg).base(bank)                      # per-bank: $8000 (lib) vs $A000 (app), fixed banks
    VD.BANK_BASE = base
    vm = NobunagaVM(rom_path=str(cfg["_rom"]), labels_path=cfg["_labels"])
    vm.mem.bank_size = cfg.get("bank_size", 0x4000); vm.mem.bank_base = base
    vm.switch_bank(bank)
    base, size = VD.bank_range(bank)
    return vm, VD, cfg, base, size, VD.find_stubs(vm.mem, base, size)


def disasm(name, bank, out=None):
    vm, VD, cfg, base, size, stubs = bank_view(name, bank)
    labels = {a: n for a, (n, _c) in bank_scoped_labels(cfg, bank).items()}
    listing, _mis = VD.render(bank, stubs, vm.mem, labels, len(vm.labels))
    if out:
        Path(out).write_text(listing, encoding="utf-8"); print(f"wrote {out} ({len(stubs)} subs)")
    else:
        sys.stdout.write(listing)


def build_ext_op_table(cfg):
    """Per-title ext-op handler table (VM opcode $B7). Decodes the address table at cfg['extop_table']
    from the ROM's fixed bank ($C000-$FFFF = last 16K of PRG) — index N -> LE word at table+2N — so the
    decompiler resolves ext-op handlers to THIS title's addresses (was a hardcoded NA2 table, which gave
    ROTK2 wrong ext_op_* names). Returns {} if not configured (caller keeps the hardcoded default)."""
    table = cfg.get("extop_table", 0)
    if not table:
        return {}
    rom = Path(cfg["_rom"]).read_bytes()
    fixed = 0x10 + (rom[4] - 1) * 0x4000               # file offset of CPU $C000 (last 16K of PRG)
    def rd(cpu):
        return rom[fixed + (cpu - 0xC000)]
    out = {}
    for i in range(48):
        a = rd(table + 2 * i) | (rd(table + 2 * i + 1) << 8)
        if a == 0xFFFF or not (0xC000 <= a <= 0xFFFF):
            if i == 0:                                 # index 0 is the unused jmp-operand slot ($0000)
                continue
            break
        if a:
            out[i] = a
    return out


def build_nop_opcodes(cfg):
    """High opcodes ($EB-$FF) that resolve to the VM's DEFAULT handler (a no-op: optional brk pad then
    JMP dispatcher) — the '37 default-handler slots'. They are inert padding in the bytecode; mapping
    them to 'nop' (suppressed) clears the misleading NA2-inherited 'trigger_syscall'/'op_FF' TODO noise.
    Game-aware: in titles where these opcodes point to REAL handlers, none match -> unchanged."""
    lo, hi, disp = cfg.get("handler_lo"), cfg.get("handler_hi"), cfg.get("dispatcher")
    if not (lo and hi and disp):
        return set()
    rom = Path(cfg["_rom"]).read_bytes()
    fixed = 0x10 + (rom[4] - 1) * 0x4000
    def rd(a): return rom[fixed + (a - 0xC000)]
    out = set()
    for op in range(0xEB, 0x100):
        h = rd(lo + op) | (rd(hi + op) << 8)
        if not (0xC000 <= h <= 0xFFFF):
            continue
        i = h
        while rd(i) == 0x00:                       # skip leading brk pad
            i += 1
        if rd(i) == 0x4C and (rd(i + 1) | rd(i + 2) << 8) == disp:   # JMP dispatcher == no-op slot
            out.add(op)
    return out


def resolve_syscall_entry_calls(text, entry_names, sysmap):
    """Some titles (e.g. L'Empereur) invoke a syscall as a native CALL to a syscall-entry trampoline
    with the syscall id as the FIRST literal operand -> the decompiler renders `syscall_entry_struct(3,
    x)` instead of the id's handler name. Rewrite `<entry>(<id literal>, args)` -> `<handler_name>(args)`
    using the title's id->name map (build_syscall_names). A non-literal first arg (computed id) is left
    untouched. General: activates only when the title configures a syscall-entry label + syscall table."""
    if not (entry_names and sysmap):
        return text
    alt = "|".join(re.escape(n) for n in entry_names)

    def rep(m):
        nm = sysmap.get(int(m.group("id")))
        if not nm:
            return m.group(0)                      # id not in table -> leave as-is
        return f"{nm}()" if m.group("sep") == ")" else f"{nm}("
    return re.sub(rf'(?:{alt})\((?P<id>\d+)(?P<sep>,\s*|\))', rep, text)


def decompiler_for(name, bank, raw=False):
    """vm_decompile configured for one bank of a title: the setup `decompile` and `callgraph` share.
    raw=True disables all structuring. -> (vm_decompile module, cfg, bank-scoped labels, syscall map)"""
    cfg = game_cfg(name); _import_core()
    import emu_harness.vm.koei.nes.vm_decompile as VC
    if raw:
        VC.STRUCTURE = False; VC.STRUCTURE_V2 = False; VC.STRUCTURE_DREAM = False
    labs = bank_scoped_labels(cfg, bank)              # addr -> (name, comment)
    sysmap = build_syscall_names(cfg, labs)           # game-aware syscall id->name (TOML-driven)
    if sysmap:
        VC.SYSCALL_TABLE = sysmap                     # override the hardcoded NA1 default
    VC.BANK_CALL_MAP = build_bank_call_map(cfg)        # resolve cross-bank trampoline targets
    VC.BANKCALL_NEEDLE = cfg.get("bankcall_fn", "call_in_bank_dcd1") + "("   # per-title trampoline name
    _et = build_ext_op_table(cfg)                       # per-title ext-op table (was hardcoded NA2 -> wrong for rot3k2)
    if _et:
        VC.EXT_OP_TABLE = _et
    if cfg.get("suppress_default_nops"):                # opt-in: opcodes -> default handler render as nop
        _nops = build_nop_opcodes(cfg)                  # (NA2-inherited 'trigger_syscall'/'op_FF' = mislabeled no-ops)
        if _nops:
            VC.OPCODE_TO_MNEMONIC = {**VC.OPCODE_TO_MNEMONIC, **{op: 'nop' for op in _nops}}
    VC.set_schema(name)                                 # per-title record field maps (na1!=na2 layout)
    return VC, cfg, labs, sysmap


def decompile(name, bank, asm_in, out=None, raw=False):
    """Batch bytecode->C for one bank's rendered VM-asm (generalizes vm_decompile_<game>.py).
    Reuses emu_harness.vm.koei.nes.vm_decompile verbatim; feeds the title's bank-scoped labels so CALL targets
    render with this game's names. `asm_in` is the listing produced by `disasm <game> <bank>`.
    raw=True disables ALL structuring -> the direct goto/label C (the `3-c-basic` baseline that the
    DREAM structurer folds into `4-c`); the two differ only by structure, same labels."""
    import io, contextlib
    VC, cfg, labs, sysmap = decompiler_for(name, bank, raw)
    text = Path(asm_in).read_text(encoding="utf-8")
    addrs = [int(x, 16) for x in re.findall(r';\s*sub\s+\$([0-9A-Fa-f]+)', text)]
    chunks, errs = [], 0
    for a in addrs:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                VC.decompile(asm_in, a, labels=labs)
            except Exception as e:
                print(f"// DECOMPILE ERROR @${a:04X}: {e}"); errs += 1
        chunks.append(buf.getvalue().rstrip())
    blob = VC.record_declarations(labs) + "\n\n".join(chunks) + "\n"
    entry_names = [labs[a][0] for k in ("syscall_entry_struct", "syscall_entry_simple")
                   for a in [cfg.get(k)] if a and a in labs]     # CALL-to-entry syscall convention
    blob = resolve_syscall_entry_calls(blob, entry_names, sysmap)
    if out:
        Path(out).write_text(blob, encoding="utf-8"); print(f"decompiled {len(addrs)} subs -> {out} ({errs} errors)")
    else:
        sys.stdout.write(blob)


def make_vm(name):
    """Return an emulator instance retargeted to <game> -- the generic version of emulator_rotk.py."""
    cfg = game_cfg(name); _import_core()
    from emu_harness.vm.koei.nes.nobunaga_vm import NobunagaVM
    disp = cfg["dispatcher"]
    class _VM(NobunagaVM):
        DISPATCHER_ADDR = disp
    vm = _VM(rom_path=str(cfg["_rom"]), labels_path=cfg["_labels"])
    vm.mem.bank_size = cfg.get("bank_size", 0x4000); vm.mem.bank_base = cfg.get("bank_base", 0x8000)
    return vm, cfg


def run_sub(game, sub, args, *, bank=None, frame_off=-16, sram=None, max_ops=20000):
    """Run one bytecode sub on the title's own interpreter; -> regL (the result register).

    Converted from na1-decompiler tools/value-oracle.py oracle_run (it certified NA1's ext-op probes):
    args go into frame slots fp+0x0B, fp+0x0D, ... (the VM calling convention), and `frame_off`
    pre-allocates locals below fp. `sub` is the stub address (its name's address); the body starts
    5 bytes on. A sub below $C000 needs its bank."""
    vm, cfg = make_vm(game)
    if sub < 0xC000 and bank is None:
        raise ValueError(f"${sub:04X} is in the switchable window: pass its bank")
    if bank is not None:
        vm.switch_bank(bank)
    if sram:
        vm.load_sram(sram)
    stub = bytes(int(b, 16) for b in cfg["vm_entry_stub"].split())
    if bytes(vm.mem.read(sub + k) for k in range(3)) != stub:
        raise ValueError(f"${sub:04X} is not a bytecode stub ({cfg['vm_entry_stub']})")
    fp = 0x05FF
    vm.vm_fp, vm.vm_sp = fp, (fp + frame_off) & 0xFFFF
    for i, x in enumerate(args):
        vm.mem.write_word(fp + 11 + 2 * i, x & 0xFFFF)
    vm.vm_pc = sub + 5
    vm.cpu.pc = vm.DISPATCHER_ADDR
    vm.run_until_outermost_return(max_ops=max_ops)
    return vm.regL


def main():
    if len(sys.argv) < 2 or sys.argv[1] == "games":
        for k, v in load_games().items():
            print(f"  {k:6} {v.get('title','')}  (dispatcher ${v['dispatcher']:04X}, vm_entry ${v['vm_entry']:04X})")
        return
    cmd = sys.argv[1]
    if cmd == "disasm":
        disasm(sys.argv[2], int(sys.argv[3], 0), sys.argv[4] if len(sys.argv) > 4 else None)
    elif cmd == "decompile":
        args = [a for a in sys.argv[2:] if a != "--raw"]; raw = "--raw" in sys.argv
        decompile(args[0], int(args[1], 0), args[2], args[3] if len(args) > 3 else None, raw=raw)
    elif cmd == "emulate":
        import argparse
        ap = argparse.ArgumentParser(prog="titles emulate", description="run one bytecode sub on the ROM's own VM")
        ap.add_argument("game")
        ap.add_argument("sub", type=lambda x: int(x, 0), help="the sub's stub address (as its name labels it)")
        ap.add_argument("args", nargs="*", type=lambda x: int(x, 0))
        ap.add_argument("--bank", type=int, help="the sub's bank (required below $C000)")
        ap.add_argument("--sram", help="an SRAM dump (.dmp) to load first")
        a = ap.parse_args(sys.argv[2:])
        r = run_sub(a.game, a.sub, a.args, bank=a.bank, sram=a.sram)
        print(f"{a.game} ${a.sub:04X}({', '.join(map(str, a.args))}) -> {r} (${r:04X})")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
