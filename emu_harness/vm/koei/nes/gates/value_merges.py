"""Value merges: no value may reach a merge unrepresented.

A register that is live into a merge carries a value the arms chose, and the C has to say which. The
decoder sweeps linearly, so a merge it does not repair keeps the fall-through arm and silently drops
the rest: the control flow stays right, so cfg_gate passes, and only a fixture on that very expression
could see it. The Give guard ($AA79) printed morale for both arms that way, and $A753 lost a table.

This gate is the structural form of that check. For every sub it takes each merge a register is live
into (vm_cfg.register_liveness: reads measured on the live handlers, writes audited), with arms that
differ, and requires one of the forms that keep the value: a ternary fold, a boolean region, or a phi.
What is left, it lists; a sub listed is one whose C may be lying.

Usage:
  py -3.14 -m emu_harness.vm.koei.nes.gates.value_merges [--game na1] [-v]     exit 1 on an open merge
"""
import argparse
import contextlib
import io
import re
import sys
import tempfile
from pathlib import Path


def open_merges(instructions):
    """-> [(merge, reg, why)] for one sub: live, differing arms, and no form that keeps the value."""
    from emu_harness.vm.koei.nes import vm_cfg as C
    cfg, leaders = C._bytecode_cfg_raw(instructions)
    block_of = C._block_of_fn(leaders)
    bmap = {L: [] for L in leaders}
    for ins in instructions:
        bmap[block_of(ins['addr'])].append(ins)
    preds = C._predecessors(cfg)
    live = C.register_liveness(instructions)
    folded = ({d['merge'] for d in C.value_diamonds(instructions)}
              | {r['merge'] for r in C.boolean_regions(instructions)})
    kept = {
        'A': (folded | {m for m, _t in C.value_merge_phis(instructions).values()}
              | {m for m, _t in C.return_phis(instructions).values()}
              | {m for m, _t in C.push_phis(instructions).values()}),
        'B': folded | {m for m, _t in C.value_merge_phis(instructions, reg='B').values()},
    }

    def sig(p):
        return tuple((i['mnemonic'], i.get('operand', '') or '') for i in bmap.get(p, []))

    out = []
    for M in leaders:
        ps = [p for p in preds.get(M, ()) if p is not C.EXIT]
        if len(ps) < 2 or len({sig(p) for p in ps}) < 2:
            continue
        for reg in live.get(M, ()):
            if M in kept[reg]:
                continue
            ends = sorted({bmap[p][-1]['mnemonic'] for p in ps if bmap.get(p)})
            out.append((M, reg, ",".join(ends)))
    return out


def census(game):
    from emu_harness.vm.koei.nes import titles, vm_decompile as VD
    cfg = titles.game_cfg(game)
    rows, subs = [], 0
    for bank in cfg["code_banks"] if "code_banks" in cfg else (0, 1, 2, 14, 15):
        with tempfile.TemporaryDirectory() as td:
            asm = Path(td) / "bank.asm"
            with contextlib.redirect_stdout(io.StringIO()):
                titles.disasm(game, bank, str(asm))
            text = asm.read_text(encoding="utf-8")
            for a in sorted({int(x, 16) for x in re.findall(r"; sub \$([0-9A-F]{4})", text)}):
                _body, ins = VD.parse_listing(str(asm), a)
                if not ins:
                    continue
                subs += 1
                for M, reg, why in open_merges(ins):
                    rows.append((bank, a, M, reg, why))
    return subs, rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Every live value at a merge is kept by the C")
    ap.add_argument("--game", default="na1")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    subs, rows = census(a.game)
    for bank, sub, M, reg, why in rows:
        print(f"  bank {bank:2} sub ${sub:04X} merge ${M:04X} reg{reg}  preds end in {why}")
    print(f"{a.game}: {subs} subs, {len(rows)} open value merges")
    print("FAIL" if rows else "PASS")
    return 1 if rows else 0


if __name__ == "__main__":
    sys.exit(main())
