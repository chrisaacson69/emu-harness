#!/usr/bin/env python3
"""probe-crossjump.py — map the REVERSIBILITY boundary of GCC's cross-jumping optimization.

Background. Flag bisection (2026-06-05) pinned the forward optimization behind NA1's
"address-inverted shared tail" residue: it is GCC's RTL CROSS-JUMPING pass (-fcrossjumping, on at
-O2+; -fno-crossjumping alone restores the -O0 duplicated form; -ftree-tail-merge is innocent).
Cross-jumping merges identical block SUFFIXES, frequently emitting a BACKWARD jmp (a later-addressed
arm jumps into an earlier-addressed surviving copy) — the layout the NA1 decompiler's address-based
gate cannot place.

The inverse atom is TAIL DUPLICATION: copy the merged suffix back into each predecessor. This probe
measures, per corpus function and opt level, WHEN cross-jumping fires and whether the merged block is
a SINK (ends in `ret` or a tail-call `jmp <symbol>`) — because that is the axis that decides whether
the duplicated form is address-monotonic (gate-emittable):

  SINK merge  + backward jmp  => the gate-breaking inversion, but the inverse (dup the sink) is
                                 provably sound AND restores address order (a sink constrains no
                                 successor ordering). REVERSIBLE + EMITTABLE (narrow gate change).
  NON-SINK merge              => GCC prefers duplication over cross-jump here; if a target compiler
                                 DID cross-jump a non-sink, dup would not restore monotonic order
                                 (the successor gains the duplicate's copies as predecessors).
  no merge (tiny tail / loop) => below cross-jump's size threshold, or a cycle block (not freely
                                 duplicable); already emittable / untouched.

Usage:  py -3 tools/probe-crossjump.py [corpus/08_crossjump_reversibility.c]
        py -3 tools/probe-crossjump.py --opts -O0,-O1,-O2,-Os --bisect
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
DEFAULT_GCC = r"C:\msys64\ucrt64\bin\gcc.exe"

FUNCS = ["A_sink2", "B_sinkN", "C_switch_sink", "D_chain_shared",
         "E_nonsink_merge", "F_loop_tail", "G_partial"]


def gcc_path():
    return os.environ.get("LA_GCC", DEFAULT_GCC)


def compile_asm(gcc, src, opts):
    tag = opts.replace(" ", "").replace("-", "")
    out = os.path.join(BUILD, f"{os.path.splitext(os.path.basename(src))[0]}_{tag}.s")
    env = os.environ.copy()
    env["PATH"] = os.path.dirname(gcc) + os.pathsep + env.get("PATH", "")
    subprocess.run([gcc, *opts.split(), "-S", os.path.abspath(src), "-o", out],
                   check=True, capture_output=True, text=True, env=env)
    return out


def fn_segment(asm_path, fn):
    lines = open(asm_path, encoding="utf-8", errors="replace").read().splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == f"{fn}:"), None)
    if start is None:
        return []
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if ".seh_endproc" in lines[i] or ".cfi_endproc" in lines[i]:
            end = i
            break
        if re.match(r"^[A-Za-z_]\w*:\s*$", lines[i]):
            end = i
            break
    return lines[start:end]


def label_positions(seg):
    pos = {}
    for i, l in enumerate(seg):
        m = re.match(r"^(\.L\d+):", l.strip())
        if m:
            pos[m.group(1)] = i
    return pos


def block_is_sink(seg, label_idx):
    """Walk forward from a label; is the block terminated by `ret` or a tail-call `jmp <symbol>`
    (sink) rather than falling/jumping to another local .L block (non-sink)?"""
    for i in range(label_idx + 1, len(seg)):
        s = seg[i].strip()
        if not s or s.startswith(".") and re.match(r"^\.(seh_|cfi_|p2align|L\d+:)", s):
            if re.match(r"^\.L\d+:", s):     # fell through to another local block => non-sink
                return False
            continue
        if s == "ret" or re.match(r"^jmp\s+[A-Za-z_]\w*\s*$", s):   # ret or tail-call
            return True
        if re.match(r"^jmp\s+\.L\d+", s):    # jumps to another local block => non-sink merge
            return False
        # any other instruction: keep walking to the terminator
    return True   # ran to end of function body => terminal


def analyze(seg):
    pos = label_positions(seg)
    sink_calls = sum(1 for l in seg if re.search(r"(call|jmp)\s+fin\b", l))
    backward = []        # (label, is_sink) for each backward local jmp = an address inversion
    forward = 0
    for i, l in enumerate(seg):
        m = re.match(r"^\s*jmp\s+(\.L\d+)", l)
        if not m:
            continue
        tgt = m.group(1)
        if tgt in pos and pos[tgt] < i:
            backward.append((tgt, block_is_sink(seg, pos[tgt])))
        elif tgt in pos:
            forward += 1
    return sink_calls, backward, forward


def verdict(sink_o0, sink_at, back_at):
    """Classify from the canonical opt cell (default -O2) vs -O0. A cross-jump = the sink-copy
    count DROPPED (merge fired); the inversion = a backward local jmp into the surviving copy."""
    if sink_o0 == 0:
        return "no sink tail in this shape (loop body share) -- not a cross-jump target"
    merged = sink_at < sink_o0
    sink_inv = [b for b in back_at if b[1]]
    nonsink_inv = [b for b in back_at if not b[1]]
    if merged and nonsink_inv:
        return "NON-SINK INVERSION (hard: dup does not restore monotonic order)"
    if merged and sink_inv:
        return "SINK INVERSION -> reversible by sink-dup, address-monotonic (narrow gate fix)"
    if merged:
        return "merged, forward-only -> already emittable (sink reached by forward edges)"
    if sink_at > sink_o0:
        return "DUPLICATED, not cross-jumped (non-sink merge) -> already emittable"
    return "no cross-jump (tail below size threshold) -> already emittable"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?",
                    default=os.path.join(ROOT, "corpus", "08_crossjump_reversibility.c"))
    ap.add_argument("--opts", default="-O0,-O1,-O2,-Os")
    ap.add_argument("--at", default="-O2", help="opt level the verdict is computed at")
    ap.add_argument("--bisect", action="store_true",
                    help="confirm -fcrossjumping is the responsible flag at -O2")
    args = ap.parse_args()

    gcc = gcc_path()
    os.makedirs(BUILD, exist_ok=True)
    opt_list = args.opts.split(",")
    if args.at not in opt_list:
        opt_list.append(args.at)
    asm = {o: compile_asm(gcc, args.src, o) for o in opt_list}

    o0 = opt_list[0]
    print(f"{os.path.basename(args.src)}   opt levels: {', '.join(opt_list)}   (verdict @ {args.at})\n")
    print(f"  {'function':16} " + "  ".join(f"{o:>10}" for o in opt_list) + f"   verdict (@ {args.at})")
    for fn in FUNCS:
        cells, sink0, sink_at, back_at = [], None, None, []
        for o in opt_list:
            sc, back, fwd = analyze(fn_segment(asm[o], fn))
            inv = sum(1 for b in back if b[1]) and "S" or (sum(1 for b in back if not b[1]) and "N" or "")
            cells.append(f"{sc}c/{len(back)}b{inv}")
            if o == o0:
                sink0 = sc
            if o == args.at:
                sink_at, back_at = sc, back
        v = verdict(sink0, sink_at, back_at)
        print(f"  {fn:16} " + "  ".join(f"{c:>10}" for c in cells) + f"   {v}")
    print("\n  legend: <N>c = copies of the sink tail (fin); <N>b = local backward jmps; "
          "S=sink-inversion, N=non-sink")

    if args.bisect:
        print("\n  flag bisection @ -O2 (which pass merges the tail?):")
        for flag in ["", "-fno-crossjumping", "-fno-tree-tail-merge"]:
            a = compile_asm(gcc, args.src, f"-O2 {flag}".strip())
            sc, _, _ = analyze(fn_segment(a, "C_switch_sink"))
            print(f"    -O2 {flag or '(none)':22} C_switch_sink sink copies = {sc}"
                  f"  {'<- restored duplication' if sc > 1 else ''}")


if __name__ == "__main__":
    sys.exit(main())
