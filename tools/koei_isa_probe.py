"""koei_isa_probe.py - each KOEI NES title's own VM handlers, measured: how many operand bytes does
an opcode really consume?

    py -3.14 tools/koei_isa_probe.py [title ...] [--ops 0xAD,0xE0,...]

For every opcode that falls through (no jump, call, switch or return), place `op, 0, 0, ...` in RAM,
point the VM instruction pointer at it, run the ROM's own dispatcher + handler for one instruction
(emu_harness.vm.koei NobunagaVM.step_one_vm_op) and read how far the instruction pointer moved.
$B7-extended opcodes are probed as `$B7, e, 0, ...`. That is the encoded length as the cartridge's
code consumes it: the lowest artifact there is short of the CPU log. "-" = the handler did not come
back to the dispatcher within the step budget (a dead slot, a BRK, a loop on the zero operand).
Compared with the NES decoder's lengths and the SNES table (tools/koei_isa_census.py). MIGRATION.md F2.
"""
import argparse
import sys

from emu_harness.vm.koei import titles
from emu_harness.vm.koei import vm_disasm as VD
from emu_harness.vm.koei.nobunaga_vm import OPCODE_INFO

sys.path.insert(0, str(titles.SIBLINGS / "koei-snes" / "tools"))
import vm_isa  # noqa: E402

BUF = 0x0300
CONTROL = {0xAC, 0xCF, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDD, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xB7}


def probe(vm, code):
    """-> bytes consumed after the first byte, or None."""
    for i in range(16):
        vm.mem.write(BUF + i, 0)
    for i, b in enumerate(code):
        vm.mem.write(BUF + i, b)
    vm.vm_pc, vm.vm_sp, vm.vm_fp = BUF, 0x05FF, 0x05F0
    vm.cpu.pc, vm.cpu.sp = vm.DISPATCHER_ADDR, 0xFD
    try:
        if vm.step_one_vm_op(max_cpu_steps=5000) is None:
            return None
    except Exception:
        return None
    return (vm.vm_pc - BUF - 1) & 0xFFFF


class Zeros:
    def __init__(self, *b):
        self.b = bytes(b) + bytes(16)

    def read(self, a):
        return self.b[a]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("titles", nargs="*", default=["na1", "ro3k", "gk", "na2", "rot3k2", "bk", "gemfire", "lemp", "uw"])
    ap.add_argument("--ops", help="comma list of primary opcodes (default: all fall-through ops)")
    ap.add_argument("--all", action="store_true", help="print rows where everyone agrees too")
    a = ap.parse_args(argv)
    T = vm_isa.TABLE
    prim = [int(x, 0) for x in a.ops.split(",")] if a.ops else [op for op in range(0x80, 0x100) if op not in CONTROL]
    rows = [("", op) for op in prim] + [("ext", e) for e in range(T.ext_count)]
    vms = {t: titles.make_vm(t)[0] for t in a.titles}
    print(f"{'opcode':10}{'NES decoder':>12}{'SNES table':>11}   measured on each NES ROM: " + " ".join(f"{t:>7}" for t in a.titles))
    for kind, op in rows:
        if kind:
            dec = VD.instr_len(Zeros(0xB7, op), 0) - 1
            s = T.extended.get(op)
            snes = "dead" if s is None else s.size + 1          # + the ext index byte
            got = [probe(v, (0xB7, op)) for v in vms.values()]
            label = f"$B7 ${op:02X}"
        else:
            dec = VD.instr_len(Zeros(op), 0) - 1
            s = T.primary.get(op)
            snes = "dead" if s is None else ("var" if s.size is None else s.size)
            got = [probe(v, (op,)) for v in vms.values()]
            label = f"${op:02X}"
        agree = all(g == dec for g in got) and (snes == dec or snes == "dead")
        if a.all or not agree:
            print(f"{label:10}{dec:>12}{str(snes):>11}   " + " ".join(f"{('-' if g is None else g):>7}" for g in got)
                  + f"   {OPCODE_INFO.get(op, ('',))[0] if not kind else ''}")


if __name__ == "__main__":
    sys.exit(main())
