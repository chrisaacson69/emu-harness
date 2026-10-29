#!/usr/bin/env python3
"""cfg-signature.py — turn GCC GIMPLE-CFG dumps into normalized CFG signatures.

For each function in a build/*.cfg dump it extracts the basic-block graph and
classifies every block by role (branch / switch / shared-merge / shared-return /
routing / loop-header / latch), then prints a readable shape descriptor plus a
bb-number-independent canonical fingerprint (so identical shapes across functions
collapse — and so an NA1 sub's observed shape can be matched against this catalog).

GCC dump conventions baked in:
  * bb 0 = virtual ENTRY, bb 1 = virtual EXIT. Real code starts at bb 2.
  * a `succs { 1 }` edge is a return-to-EXIT; we render EXIT as 'X'.
  * "Loop 0" is the synthetic whole-function loop; real loops have depth >= 1.
  * a switch's succ order lists `default` FIRST, then cases in source order.

Usage:  py -3 tools/cfg-signature.py [build/foo.cfg ...]   (default: all build/*.cfg)
        py -3 tools/cfg-signature.py --quiet                (one fingerprint line each)
"""
import argparse
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
EXIT = "X"  # virtual EXIT (bb 1)

RE_FUNC = re.compile(r"^;; Function (\S+) ")
RE_LOOP = re.compile(r"^;; Loop (\d+)\b")
RE_HEADER = re.compile(r"header (\d+), latch (\d+)")
RE_DEPTH = re.compile(r"depth (\d+), outer (-?\d+)")
RE_SUCCS = re.compile(r"^;;\s+(\d+) succs \{([^}]*)\}")
RE_BB = re.compile(r"^\s*<bb (\d+)>")
RE_LABEL = re.compile(r"^<L\d+>:")


def split_functions(text):
    """Yield (name, body_lines) per function block in a .cfg dump."""
    chunks = re.split(r"(?=^;; Function )", text, flags=re.M)
    for ch in chunks:
        m = RE_FUNC.search(ch)
        if m:
            yield m.group(1), ch.splitlines()


def is_real_stmt(line):
    """A 'real' statement = content a faithful decompile must emit verbatim
    (assignment / call / return-value), as opposed to control scaffolding."""
    s = line.strip()
    if not s:
        return False
    if s.startswith((";;", "//", "<bb", "{", "}", "[")):
        return False
    if RE_LABEL.match(s):
        return False
    if s.startswith(("goto ", "if (", "else", "switch ")):
        return False
    if s == "[INV]":
        return False
    return True


def parse_function(name, lines):
    loops = {}          # id -> {header, latch, depth, nodes}
    succs = {}          # bb -> [targets] ('1' becomes EXIT)
    bodies = {}         # bb -> list of statement lines
    has_switch = set()  # bbs whose terminator is a switch

    cur_loop = None
    cur_bb = None
    for ln in lines:
        ml = RE_LOOP.match(ln)
        if ml:
            cur_loop = int(ml.group(1))
            loops[cur_loop] = {"header": None, "latch": None, "depth": None, "nodes": []}
            continue
        if cur_loop is not None:
            mh = RE_HEADER.search(ln)
            if mh:
                loops[cur_loop]["header"] = int(mh.group(1))
                loops[cur_loop]["latch"] = int(mh.group(2))
                continue
            md = RE_DEPTH.search(ln)
            if md:
                loops[cur_loop]["depth"] = int(md.group(1))
                continue
            if "nodes:" in ln:
                loops[cur_loop]["nodes"] = [int(x) for x in ln.split("nodes:")[1].split()]
                cur_loop = None
                continue
        ms = RE_SUCCS.match(ln)
        if ms:
            src = int(ms.group(1))
            tgts = [EXIT if t == "1" else int(t) for t in ms.group(2).split()]
            succs[src] = tgts
            continue
        mb = RE_BB.match(ln)
        if mb:
            cur_bb = int(mb.group(1))
            bodies.setdefault(cur_bb, [])
            continue
        if cur_bb is not None:
            if "switch (" in ln:
                has_switch.add(cur_bb)
            if is_real_stmt(ln):
                bodies[cur_bb].append(ln.strip())

    real_loops = {i: l for i, l in loops.items() if (l["depth"] or 0) >= 1}
    nodes = sorted(succs)                      # real bbs (>=2)
    indeg = {n: 0 for n in nodes}
    indeg[EXIT] = 0
    for s, ts in succs.items():
        for t in ts:
            indeg[t] = indeg.get(t, 0) + 1
    backedges = {(l["latch"], l["header"]) for l in real_loops.values()}

    entry = min(nodes) if nodes else None
    roles = {}
    for n in nodes:
        out = succs[n]
        nstmt = len(bodies.get(n, []))
        if n in has_switch:
            kind = "switch"
        elif out == [EXIT]:
            kind = "return"
        elif len(out) >= 2:
            kind = "branch"
        elif len(out) == 1 and nstmt == 0:
            kind = "routing"     # NA1 atom-3: pure goto-forwarding stub
        else:
            kind = "linear"
        roles[n] = {"kind": kind, "out": out, "nstmt": nstmt, "indeg": indeg[n]}

    return {
        "name": name, "entry": entry, "nodes": nodes, "succs": succs,
        "indeg": indeg, "roles": roles, "real_loops": real_loops,
        "backedges": backedges, "has_switch": has_switch,
    }


def fingerprint(fn):
    """Canonical, bb-number-independent structural signature: DFS from entry in
    successor (semantic: then/else/case) order, relabel by visit order, emit each
    node's role + edge kinds. Back-edges marked '^', exit '>X'."""
    succs, roles, back = fn["succs"], fn["roles"], fn["backedges"]
    order, seen = [], {}
    stack = [fn["entry"]] if fn["entry"] is not None else []
    while stack:
        n = stack.pop()
        if n == EXIT or n in seen:
            continue
        seen[n] = len(order)
        order.append(n)
        for t in reversed(succs.get(n, [])):
            if t != EXIT and t not in seen:
                stack.append(t)
    relabel = {n: i for i, n in enumerate(order)}
    parts = []
    for n in order:
        edges = []
        for t in succs.get(n, []):
            if t == EXIT:
                edges.append("X")
            elif (n, t) in back:
                edges.append(f"^{relabel[t]}")
            else:
                edges.append(str(relabel.get(t, "?")))
        parts.append(f"{roles[n]['kind'][0]}{relabel[n]}>{','.join(edges)}")
    return " ".join(parts)


def describe(fn):
    branch = [n for n in fn["nodes"] if fn["roles"][n]["kind"] == "branch"]
    switch = sorted(fn["has_switch"])
    routing = [n for n in fn["nodes"] if fn["roles"][n]["kind"] == "routing"]
    merges = [n for n in fn["nodes"] if fn["indeg"][n] >= 2]
    shared_ret = [n for n in fn["nodes"]
                  if fn["roles"][n]["kind"] == "return" and fn["indeg"][n] >= 2]
    bits = []
    if fn["real_loops"]:
        ld = ", ".join(f"L{i}(hdr {l['header']},latch {l['latch']},depth {l['depth']})"
                       for i, l in sorted(fn["real_loops"].items()))
        bits.append(f"loops: {ld}")
    if switch:
        bits.append(f"switch@{switch} (n-way)")
    if branch:
        bits.append(f"2-way branches@{branch}")
    if merges:
        bits.append(f"merges(in>=2)@{merges}")
    if shared_ret:
        bits.append(f"SHARED-RETURN@{shared_ret}")
    if routing:
        bits.append(f"ROUTING(goto-only)@{routing}")
    return bits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--quiet", action="store_true", help="one fingerprint line per function")
    args = ap.parse_args()

    files = args.files or sorted(glob.glob(os.path.join(BUILD, "*.cfg")))
    if not files:
        sys.exit("no .cfg dumps — run tools/compile-dumps.py first.")

    for path in files:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if not args.quiet:
            print(f"\n=== {os.path.basename(path)} " + "=" * (40 - len(os.path.basename(path))))
        for name, lines in split_functions(text):
            fn = parse_function(name, lines)
            fp = fingerprint(fn)
            if args.quiet:
                print(f"{name:24s} {fp}")
                continue
            edges = ", ".join(
                f"{n}->{'/'.join(str(t) for t in fn['succs'][n])}" for n in fn["nodes"])
            print(f"\n  {name}  ({len(fn['nodes'])} blocks, entry bb{fn['entry']})")
            for b in describe(fn):
                print(f"    - {b}")
            print(f"    edges:  {edges}")
            print(f"    sig:    {fp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
