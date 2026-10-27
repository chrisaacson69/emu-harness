"""Compare-opcode probe: the direction and signedness of $C0-$C9, measured on the live handler.

The primitive-layer check the structural gates cannot make. A wrong compare direction changes
neither the bytecode (round-trip identical) nor the CFG (the orientation flips the same way in the
raw and the structured C), so every other gate stays green while the C inverts every threshold.
NA1 found 4 such ops at 323 sites this way (2026-06-04). Converted from na1-decompiler
`tools/attic/probe-compare-opcodes.py` (2026-10-27), where it had been atticked and could no longer
import; this copy runs any title, reads the shared opcode table, and fails if it cannot read it.

Each compare runs through the cartridge's own dispatcher for 5 (regL, regR) pairs chosen so the 10
candidate predicates give distinct truth vectors: 3<6 / 6>3 / 6==6 fix the direction, (-1,1) /
(1,-1) split signed from unsigned. The measured predicate must match the decompiler's mnemonic and
`_BINOP` entry and the table's mnemonic.

Usage:
  py -3.14 -m emu_harness.vm.koei.nes.gates.compare_probe              # NA1
  py -3.14 -m emu_harness.vm.koei.nes.gates.compare_probe --game gk    # any title
  py -3.14 -m emu_harness.vm.koei.nes.gates.compare_probe --all        # every title in games.toml
"""
import argparse
import sys
import tomllib
from pathlib import Path

FP, SP = 0x0500, 0x04C0
MATRIX = [("3<6", 3, 6), ("6>3", 6, 3), ("6==6", 6, 6), ("-1,1", 0xFFFF, 1), ("1,-1", 1, 0xFFFF)]
OPS = range(0xC0, 0xCA)            # the binary compares ($CA is is_zero, unary)


def _s(v):
    return v - 0x10000 if v >= 0x8000 else v


PREDS = {
    'eq':  lambda l, r: l == r,          'ne':  lambda l, r: l != r,
    'slt': lambda l, r: _s(l) < _s(r),   'sle': lambda l, r: _s(l) <= _s(r),
    'sgt': lambda l, r: _s(l) > _s(r),   'sge': lambda l, r: _s(l) >= _s(r),
    'ult': lambda l, r: l < r,           'ule': lambda l, r: l <= r,
    'ugt': lambda l, r: l > r,           'uge': lambda l, r: l >= r,
}
# predicate -> (decompiler mnemonic, C symbol, signedness, table mnemonic)
CANON = {
    'eq':  ('cmp_eq', '==', 'none', 'CMPEQ'),   'ne':  ('cmp_ne', '!=', 'none', 'CMPNE'),
    'slt': ('cmp_slt', '<', 'none', 'SCMPLT'),  'sle': ('cmp_sle', '<=', 'none', 'SCMPLE'),
    'sgt': ('cmp_sgt', '>', 'none', 'SCMPGT'),  'sge': ('cmp_sge', '>=', 'none', 'SCMPGE'),
    'ult': ('cmp_ult', '<', 'both', 'UCMPLT'),  'ule': ('cmp_ule', '<=', 'both', 'UCMPLE'),
    'ugt': ('cmp_ugt', '>', 'both', 'UCMPGT'),  'uge': ('cmp_uge', '>=', 'both', 'UCMPGE'),
}
VEC2PRED = {tuple(int(f(l, r)) for _, l, r in MATRIX): p for p, f in PREDS.items()}


def measure(vm, op):
    """Run one compare through the real handler for each matrix pair -> truth vector."""
    out = []
    for _, l, r in MATRIX:
        for a in range(0x0400, 0x0540):
            vm.mem.write(a, 0)
        vm.mem.write(0x0400, op)
        vm.vm_fp, vm.vm_sp = FP, SP
        vm.regL, vm.regR = l & 0xFFFF, r & 0xFFFF
        vm.vm_pc = 0x0400
        vm.vm_call_depth = 0
        vm.cpu.pc = vm.DISPATCHER_ADDR
        vm.step_one_vm_op(max_cpu_steps=4000)
        out.append(1 if vm.regL else 0)
    return tuple(out)


def table_mnemonics():
    """{opcode: mnemonic} from the shared table; raises if it is missing (a skipped half is not a pass)."""
    from emu_harness.vm.koei.nes import nobunaga_vm
    data = tomllib.loads(Path(nobunaga_vm.OPCODES_PATH).read_text(encoding="utf-8"))
    return {int(k[3:], 16): v["mnemonic"] for k, v in data.items() if k.startswith("op_") and "mnemonic" in v}


def check(game="na1", dec_map=None, binop=None):
    """-> [(op, predicate or None, ok, detail)]. dec_map / binop default to the decompiler's own."""
    from emu_harness.vm.koei.nes import titles, vm_decompile
    vm, _cfg = titles.make_vm(game)
    dec_map = vm_decompile.OPCODE_TO_MNEMONIC if dec_map is None else dec_map
    binop = vm_decompile._BINOP if binop is None else binop
    table = table_mnemonics()
    rows = []
    for op in OPS:
        try:
            pred = VEC2PRED.get(measure(vm, op))
        except Exception as ex:                     # the handler itself failed: report, never pass
            rows.append((op, None, False, f"exec error: {type(ex).__name__}: {ex}"))
            continue
        if pred is None:
            rows.append((op, None, False, "not a recognized compare"))
            continue
        want_mn, want_sym, want_sign, want_tbl = CANON[pred]
        got_mn = dec_map.get(op, "<none>")
        got_sym, got_sign = binop.get(got_mn, ("<?>", "<?>"))
        got_tbl = table.get(op, "<none>")
        ok = (got_mn, got_sym, got_sign, got_tbl) == (want_mn, want_sym, want_sign, want_tbl)
        rows.append((op, pred, ok, f"decompiler={got_mn}({got_sym}/{got_sign}) table={got_tbl}"))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Compare-opcode direction/signedness vs the live handler")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--game", default="na1", help="a title profile (games.toml); default na1")
    g.add_argument("--all", action="store_true", help="every title in games.toml")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    from emu_harness.vm.koei.nes import titles
    games = list(titles.load_games()) if a.all else [a.game]
    bad = 0
    for game in games:
        rows = check(game)
        nbad = sum(not ok for *_, ok, _ in rows)
        bad += nbad
        print(f"{game}: {len(rows) - nbad}/{len(rows)} compares match the live handler")
        for op, pred, ok, detail in rows:
            if a.verbose or not ok:
                print(f"  ${op:02X}: {'OK ' if ok else 'BAD'} measured={pred}  {detail}")
    print("PASS" if not bad else f"FAIL: {bad} compare(s) disagree with the handler")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
