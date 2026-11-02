"""
tools/vm-cfg-gate.py — the CFG equivalence gate (control-flow structuring epic, Phase 0).

Runs the whole 4-bank codebase through two checks:

  (1) WITNESS FAITHFULNESS (hard gate, exit 1 on any failure):
      lower_c_to_cfg(RAW goto witness) == bytecode_cfg, for every sub.
      Proves `decompile-all.py --raw` emits a CFG-faithful round-trip witness — the
      reversibility guarantee. Also validates the bytecode CFG extractor on real data.
      Plus: no unrecognised control-shaped mnemonics slipped through.

  (2) STRUCTURED EQUIVALENCE (informational in Phase 0):
      The structured C the decompiler emits, per sub, by the relation that matches its owner:
      DREAM via the AST-native reorder-tolerant `dream_equivalent_ast`, the region-reducer
      fallback via `structured_equivalent` (contraction of pure-routing blocks, return-value
      EXIT edges, true/false edge labels), the honest goto form trivially. This gates the
      committed bank_NN.c.

  (3) FORMULA OVERRIDES: a sub printed as a formula has no CFG; its model is run against the
      ROM's own sub over an edge grid.

Usage:
  py -3 tools/vm-cfg-gate.py [--banks 0,1,2,15] [--verbose] [--show-structured]
"""
import argparse
import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path


from emu_harness.vm.koei.nes import vm_cfg
from emu_harness.vm.koei.nes import vm_decompile
from emu_harness.vm.koei.nes import dream
from emu_harness.vm.koei.nes.cli import decompile_all

CODE_BANKS = [0, 1, 2, 10, 14, 15]


def _owner_equiv(collect, leaders):
    """Per-sub equivalence of the emitted structured C. Each sub is validated with the relation
    that matches its OWNER (recorded by decompile() as collect['structurer']): DREAM-owned subs
    via the AST-native reorder-tolerant `dream_equivalent_ast` (re-deriving the AST from the raw
    witness exactly as the emit did — the address-anchored gate would falsely reject DREAM's
    reorder-inverted merges); region-reducer subs via `structured_equivalent`; the honest goto
    form is trivially the witness. Returns (ok, n_raw, n_str)."""
    who = collect.get('structurer')
    raw, instrs = collect['raw'], collect['instructions']
    if who == 'dream':
        built = dream.dream_ast(raw, instrs)
        if built is None:
            return False, None, None
        ast, tc, nxt = built
        try:
            return bool(dream.dream_equivalent_ast(raw, ast, tc, tc['leaders'])), None, None
        except Exception:
            return False, None, None
    if who == 'raw':
        return True, None, None
    # The region-reducer fallback: the address-anchored gate is correct.
    return vm_cfg.structured_equivalent(raw, collect['structured'], leaders)


def _fmt(cfg):
    parts = []
    for L in sorted(cfg, key=lambda x: (x is vm_cfg.EXIT, x)):
        succ = ",".join(s if s is vm_cfg.EXIT else f"${s:04X}"
                        for s in sorted(cfg[L], key=lambda x: (x is vm_cfg.EXIT, x)))
        parts.append(f"    ${L:04X} -> {{{succ}}}")
    return "\n".join(parts)


EDGE_VALUES = (0, 1, 2, 3, 7, 9, 10, 11, 99, 100, 255, 1000, 6553, 6554, 0x7FFF, 0x8000, 0xFFFF)


def override_value_check(stub, values=EDGE_VALUES):
    """-> [(args, rom, model)] where an override's model disagrees with the ROM's own sub."""
    import itertools
    from emu_harness.vm.koei.nes import vm_decompile
    from emu_harness.vm.koei.nes.titles import run_sub
    _c, model, bank = vm_decompile._EXTOP_OVERRIDE[stub]
    n = model.__code__.co_argcount
    bad = []
    for args in itertools.product(values, repeat=n):
        rom, want = run_sub("na1", stub, list(args), bank=bank), model(*args)
        if rom != want:
            bad.append((args, rom, want))
    return bad


def run(banks, verbose=False, show_structured=False):
    raw_fail = []        # lower(raw) != bytecode                    — HARD (witness faithful)
    struct_fail = []     # the GATED structured output != raw        — HARD (gating regression)
    fellback = []        # subs where the reducer's fold was rejected -> honest goto (expected)
    owners = {}          # structurer -> count (dream / reducer / raw)
    override_ok, override_fail = [], []   # formula overrides, witnessed by value against the ROM
    unknown_mn = []      # (bank, sub, addr, mnemonic) — unrecognised control op
    n_subs = 0

    with tempfile.TemporaryDirectory() as td:
        for bank in banks:
            results = {}

            def on_sub(stub, collect, _bank=bank, _results=results):
                _results[stub] = collect

            buf = io.StringIO()
            with redirect_stdout(buf):
                decompile_all.bank_subs(bank, Path(td), on_sub=on_sub)

            for stub, collect in sorted(results.items()):
                n_subs += 1
                if collect.get('structurer') == 'override':
                    # A printed formula has no CFG to compare: its witness is its value, the
                    # model against the ROM's own sub over an edge grid (vm_decompile overrides).
                    bad = override_value_check(stub)
                    (override_fail if bad else override_ok).append((bank, stub))
                    if bad and verbose:
                        print(f"\nOVERRIDE != ROM  bank {bank} ${stub:04X}: {bad[:4]}")
                    continue
                instrs = collect['instructions']
                bc, leaders = vm_cfg.bytecode_cfg(instrs)
                for a, mn in vm_cfg.unknown_control_mnemonics(instrs):
                    unknown_mn.append((bank, stub, a, mn))
                cc_raw = vm_cfg.lower_goto_cfg(collect['raw'], leaders)
                if cc_raw != bc:
                    raw_fail.append((bank, stub))
                    if verbose:
                        print(f"\nRAW != bytecode  bank {bank} ${stub:04X}")
                        print("  bytecode:\n" + _fmt(bc))
                        print("  lower(raw):\n" + _fmt(cc_raw))
                # The GATED output decompile actually emits must be CFG-equivalent, each sub
                # checked by its owner's relation.
                who = collect.get('structurer')
                owners[who] = owners.get(who, 0) + 1
                ok, n_raw, n_str = _owner_equiv(collect, leaders)
                if not ok:
                    struct_fail.append((bank, stub))
                    if show_structured:
                        print(f"\nGATED STRUCT !~= raw  bank {bank} ${stub:04X}")
                        print("  contracted raw:\n" + _fmt(n_raw))
                        print("  contracted structured:\n" + _fmt(n_str))
                if collect.get('gated_fallback'):
                    fellback.append((bank, stub))

    n_ov = len(override_ok) + len(override_fail)
    n_cfg = n_subs - n_ov                     # the CFG checks cover the subs that print a body
    print(f"\nCFG gate over {n_subs} subs in banks {banks} ({n_ov} printed as formula overrides):")
    print(f"  (1) witness faithfulness   lower(raw) == bytecode : "
          f"{n_cfg - len(raw_fail)}/{n_cfg} pass"
          + ("" if not raw_fail else f"  — {len(raw_fail)} FAIL"))
    print(f"  (2) gated structuring is CFG-preserving           : "
          f"{n_cfg - len(struct_fail)}/{n_cfg} pass"
          + ("" if not struct_fail else f"  — {len(struct_fail)} FAIL"))
    print(f"      owners: DREAM {owners.get('dream', 0)}, region reducer {owners.get('reducer', 0)}, "
          f"honest goto {owners.get('raw', 0)} ({len(fellback)} of them a rejected reducer fold)")
    if n_ov:
        print(f"  (3) formula overrides == ROM by value (edge grid) : {len(override_ok)}/{n_ov} pass"
              + ("" if not override_fail else f"  — {len(override_fail)} FAIL"))
    if unknown_mn:
        print(f"  ! {len(unknown_mn)} unrecognised control mnemonic(s):")
        for bank, sub, a, mn in unknown_mn[:20]:
            print(f"      bank {bank} ${sub:04X} @ ${a:04X}: {mn}")

    hard_fail = bool(raw_fail) or bool(struct_fail) or bool(unknown_mn) or bool(override_fail)
    if hard_fail:
        print("\nHARD GATE FAILED.")
        if raw_fail:
            print("  raw-witness CFG diverged from bytecode at: "
                  + ", ".join(f"b{b}/${s:04X}" for b, s in raw_fail[:20]))
        if override_fail:
            print("  formula override disagrees with the ROM at: "
                  + ", ".join(f"b{b}/${s:04X}" for b, s in override_fail))
        if struct_fail:
            print("  GATED structured output is NOT CFG-preserving at (gating bug): "
                  + ", ".join(f"b{b}/${s:04X}" for b, s in struct_fail[:20]))
        return 1
    print("\nHARD GATE CLEAN — the --raw witness is CFG-faithful to the bytecode, AND "
          "every emitted structured fold is CFG-preserving (else it fell back to goto).")
    return 0


def main():
    ap = argparse.ArgumentParser(description="CFG equivalence gate (structuring epic)")
    ap.add_argument("--banks", default=",".join(map(str, CODE_BANKS)))
    ap.add_argument("--verbose", action="store_true", help="dump each raw-gate failure")
    ap.add_argument("--show-structured", action="store_true",
                    help="dump each gated-structured/raw difference (a gating regression)")
    args = ap.parse_args()
    banks = [int(b, 0) for b in args.banks.split(",") if b.strip()]
    vm_decompile.STRUCTURE = True
    sys.exit(run(banks, args.verbose, args.show_structured))


if __name__ == "__main__":
    main()
