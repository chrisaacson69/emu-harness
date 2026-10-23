"""
tools/vm-cfg-gate.py — the CFG equivalence gate (control-flow structuring epic, Phase 0).

Runs the whole 4-bank codebase through two checks:

  (1) WITNESS FAITHFULNESS (hard gate, exit 1 on any failure):
      lower_c_to_cfg(RAW goto witness) == bytecode_cfg, for every sub.
      Proves `decompile-all.py --raw` emits a CFG-faithful round-trip witness — the
      reversibility guarantee. Also validates the bytecode CFG extractor on real data.
      Plus: no unrecognised control-shaped mnemonics slipped through.

  (2) STRUCTURED EQUIVALENCE (informational in Phase 0):
      lower_c_to_cfg(STRUCTURED) vs bytecode_cfg. Reports how many subs match exactly
      and characterises the residue. structure_lines already performs destructive folds
      (early-return merges a return block; empty-arm if/else collapses a diamond) that
      change the block partition while preserving behaviour — so the EXACT block gate is
      deliberately too strict here. The precise equivalence relation that licenses Phase 1+
      folds (contraction of pure-routing blocks, return-value-carrying EXIT edges,
      true/false edge labels to catch condition inversion) is the next decision to lock.

Usage:
  py -3 tools/vm-cfg-gate.py [--banks 0,1,2,15] [--verbose] [--show-structured]
"""
import argparse
import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path


from emu_harness.vm.koei import vm_cfg
from emu_harness.vm.koei import vm_decompile
from emu_harness.vm.koei import dream
from emu_harness.vm.koei.cli import decompile_all

CODE_BANKS = [0, 1, 2, 10, 14, 15]


def _hybrid_equiv(collect, leaders):
    """Per-sub equivalence for the DREAM-hybrid canonical output. Each sub is validated with
    the relation that matches its OWNER (recorded by decompile() as collect['structurer']):
    DREAM-owned subs via the AST-native reorder-tolerant `dream_equivalent_ast` (re-deriving
    the AST from the raw witness exactly as the emit did — the address-anchored gate would
    falsely reject DREAM's reorder-inverted merges); V2-fallback subs via `structured_equivalent`;
    raw passthrough is trivially the witness. Returns (ok, n_raw, n_str)."""
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
    # V2-fallback (or any non-DREAM run): the address-anchored gate is correct.
    return vm_cfg.structured_equivalent(raw, collect['structured'], leaders)


def _fmt(cfg):
    parts = []
    for L in sorted(cfg, key=lambda x: (x is vm_cfg.EXIT, x)):
        succ = ",".join(s if s is vm_cfg.EXIT else f"${s:04X}"
                        for s in sorted(cfg[L], key=lambda x: (x is vm_cfg.EXIT, x)))
        parts.append(f"    ${L:04X} -> {{{succ}}}")
    return "\n".join(parts)


def run(banks, verbose=False, show_structured=False, hybrid=False):
    raw_fail = []        # lower(raw) != bytecode                    — HARD (witness faithful)
    struct_fail = []     # the GATED structured output != raw        — HARD (gating regression)
    fellback = []        # subs where structure_lines was rejected -> honest goto (expected)
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
                # The GATED output decompile actually emits must be CFG-equivalent. In hybrid
                # (--dream) mode each sub is checked by its owner's relation; else the
                # address-anchored gate validates the active single structurer (V1/V2).
                if hybrid:
                    ok, n_raw, n_str = _hybrid_equiv(collect, leaders)
                else:
                    ok, n_raw, n_str = vm_cfg.structured_equivalent(
                        collect['raw'], collect['structured'], leaders)
                if not ok:
                    struct_fail.append((bank, stub))
                    if show_structured:
                        print(f"\nGATED STRUCT !~= raw  bank {bank} ${stub:04X}")
                        print("  contracted raw:\n" + _fmt(n_raw))
                        print("  contracted structured:\n" + _fmt(n_str))
                if collect.get('gated_fallback'):
                    fellback.append((bank, stub))

    print(f"\nCFG gate over {n_subs} subs in banks {banks}:")
    print(f"  (1) witness faithfulness   lower(raw) == bytecode : "
          f"{n_subs - len(raw_fail)}/{n_subs} pass"
          + ("" if not raw_fail else f"  — {len(raw_fail)} FAIL"))
    print(f"  (2) gated structuring is CFG-preserving           : "
          f"{n_subs - len(struct_fail)}/{n_subs} pass"
          + ("" if not struct_fail else f"  — {len(struct_fail)} FAIL"))
    print(f"      ({len(fellback)} subs fell back to honest goto — structure_lines was "
          f"not CFG-preserving there)")
    if unknown_mn:
        print(f"  ! {len(unknown_mn)} unrecognised control mnemonic(s):")
        for bank, sub, a, mn in unknown_mn[:20]:
            print(f"      bank {bank} ${sub:04X} @ ${a:04X}: {mn}")

    hard_fail = bool(raw_fail) or bool(struct_fail) or bool(unknown_mn)
    if hard_fail:
        print("\nHARD GATE FAILED.")
        if raw_fail:
            print("  raw-witness CFG diverged from bytecode at: "
                  + ", ".join(f"b{b}/${s:04X}" for b, s in raw_fail[:20]))
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
    ap.add_argument("--v2", action="store_true",
                    help="validate the V2 region reducer (vm_reduce) instead of the templates")
    ap.add_argument("--dream", action="store_true",
                    help="validate the CANONICAL DREAM-hybrid output (DREAM primary + V2 fallback): "
                         "each sub checked by its owner's relation (DREAM via dream_equivalent_ast, "
                         "V2 via structured_equivalent). This gates the actual committed bank_NN.c.")
    args = ap.parse_args()
    if args.v2 and args.dream:
        ap.error("--v2 and --dream are mutually exclusive structurer modes")
    banks = [int(b, 0) for b in args.banks.split(",") if b.strip()]
    vm_decompile.STRUCTURE_V2 = args.v2
    if args.dream:
        vm_decompile.STRUCTURE_DREAM = True
        vm_decompile.structure_dream = dream.dream_structure_gated
    sys.exit(run(banks, args.verbose, args.show_structured, hybrid=args.dream))


if __name__ == "__main__":
    main()
