"""koei_isa_census.py - the KOEI VM instruction set as the NES toolchain decodes it vs the SNES table.

    py -3.14 tools/koei_isa_census.py [--all]

The two toolchains grew independently over one engine (koei-snes docs/koei-nes-vs-snes.md: "opcode
numbers line up"). This lists, per primary opcode and per $B7-extended opcode, what each side says:
  NES   the encoded length emu_harness.vm.koei.nes.vm_disasm.instr_len uses (with zero operand bytes;
        a switch is "var"), the mnemonic (nobunaga_vm.OPCODE_INFO), and the control-flow class its
        decoder implies (vm_disasm's terminator / branch / call sets)
  SNES  the shared table, vm/koei/vm_opcode_names.toml, through its SNES loader (vm_isa): name, cf, size, dead
and classifies each as agree / size differs / class differs / dead on one side. The SNES table is
ROM-verified on 3 SNES titles (vm_isa.bind raises on any mismatch); the NES lengths are the ones the
NES decoder tiles 9 titles' banks with. A difference is a question for the handlers, not a verdict.
MIGRATION.md row F2.
"""
import argparse
import sys
from pathlib import Path

from emu_harness.vm.koei.nes import vm_disasm as VD
from emu_harness.vm.koei.nes.nobunaga_vm import OPCODE_INFO

from emu_harness.vm.koei.snes import vm_isa  # noqa: E402  (the shared table, through its SNES loader)

NES_CF = {0xCF: "ret", 0xD6: "jmp", 0xD7: "cjmp", 0xD8: "cjmp", 0xE3: "bra", 0xE6: "bra",
          0xE4: "cbra", 0xE5: "cbra", 0xE7: "cbra", 0xE8: "cbra", 0xD5: "switch", 0xD9: "switch",
          0xAC: "call", 0xE9: "call", 0xDD: "call", 0xEA: "call", 0xB7: "prefix"}


class Zeros:
    """A one-instruction memory: the opcode, then zero operand bytes."""
    def __init__(self, *b):
        self.b = bytes(b) + bytes(16)

    def read(self, a):
        return self.b[a]


def nes(op):
    name = OPCODE_INFO.get(op, ("(none)", 0))[0]
    size = "var" if op in (0xD5, 0xD9) else VD.instr_len(Zeros(op), 0) - 1
    return name, NES_CF.get(op, "none"), size


def nes_ext(e):
    return VD.instr_len(Zeros(0xB7, e), 0) - 2     # bytes after the ext index


def snes_effect(o):
    """(pops, pushes, writes regA, writes regC) in words, from an SNES op's lift template."""
    from emu_harness.vm.koei.isa import RA, RC, parse_template, step_exprs, template_effects, template_reads
    steps = parse_template(o.c)
    pops = sum(template_reads(e)[2] for st in steps for e in step_exprs(st)) // 2
    pushes = sum(st[1] for st in steps if st[0] == "push") // 2
    _use, dfn = template_effects(steps)
    return pops, pushes, bool(dfn & RA), bool(dfn & RC)


def effects(show_all=False):
    """NES vm_stack_effect (audited on the NES handlers by gates/stack_audit) vs the SNES lift
    templates (checked on the SNES handlers by vm_isa.bind), for every fall-through opcode both
    describe. regL/regR (NES) are regA/regC (SNES)."""
    from emu_harness.vm.koei.nes.vm_stack_effect import STACK_EFFECT
    T = vm_isa.TABLE
    agree, rows = 0, []
    for op, e in sorted(STACK_EFFECT.items()):
        o = T.primary.get(op)
        if e.cls not in ("data", "push", "pop", "store") or o is None or o.cf != "none" or not o.c:
            continue
        nes_e = (e.pops, e.pushes, e.regL == "w", e.regR == "w")
        sn = snes_effect(o)
        if nes_e == sn:
            agree += 1
        if show_all or nes_e != sn:
            rows.append(f"  ${op:02X} NES {e.mnem:16} pops/pushes/wL/wR {nes_e}   SNES {o.name!r} {sn}  c={o.c!r}")
    print(f"stack effects: {agree} agree, {len(rows) if not show_all else len(rows) - agree} differ")
    print("\n".join(rows))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true", help="print the agreeing opcodes too")
    ap.add_argument("--effects", action="store_true", help="compare stack / register effects instead of lengths")
    a = ap.parse_args(argv)
    if a.effects:
        return effects(a.all)
    T = vm_isa.TABLE
    tally = {}
    rows = []
    for op in range(256):
        n_name, n_cf, n_size = nes(op)
        s = T.primary.get(op)
        if s is None:
            kind = "SNES dead" + ("" if op in OPCODE_INFO else ", NES unlisted")
            s_desc = "dead"
        else:
            s_size = "var" if s.size is None else s.size
            s_desc = f"{s.name!r} cf={s.cf} size={s_size}"
            if op not in OPCODE_INFO and op >= 0x80:
                kind = "NES unlisted"
            elif s_size != n_size and s.cf != n_cf:
                kind = "size + class differ"
            elif s_size != n_size:
                kind = "size differs"
            elif s.cf != n_cf:
                kind = "class differs"
            else:
                kind = "agree"
        tally[kind] = tally.get(kind, 0) + 1
        rows.append((kind, f"${op:02X}", f"NES {n_name} cf={n_cf} size={n_size}", "SNES " + s_desc))
    for e in range(T.ext_count):
        s = T.extended.get(e)
        n_size = nes_ext(e)
        if s is None:
            kind, s_desc = "ext: SNES dead", "dead"
        else:
            kind = "ext: agree" if s.size == n_size else "ext: size differs"
            s_desc = f"{s.name!r} size={s.size}"
        tally[kind] = tally.get(kind, 0) + 1
        rows.append((kind, f"$B7 ${e:02X}", f"NES size={n_size}", "SNES " + s_desc))
    print("tally:", ", ".join(f"{k}: {v}" for k, v in sorted(tally.items(), key=lambda kv: -kv[1])))
    for kind, op, n, s in rows:
        if a.all or "agree" not in kind:
            print(f"  {op:9} {kind:22} {n:44} {s}")


if __name__ == "__main__":
    sys.exit(main())
