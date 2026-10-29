#!/usr/bin/env python3
"""probe-opt-levels.py — is an NA1 decompiler atom a base LOWERING or an inverse-OPTIMIZATION?

Compile one corpus file at -O0 AND -O2 and diff the shared-tail structure. The finding that
motivated this (2026-06-05): the $AD38 "address-inverted shared tail" the V2 reducer can't place
is a TAIL-MERGE / CROSS-JUMP optimization — it appears at -O2, never at -O0. At -O0 GCC DUPLICATES
the common tail (one copy per arm, address-ordered = the decompiler's 0-goto target). So atom-5
(terminal duplication) is the INVERSE of cross-jumping, not the inverse of a lowering — and the -O0
output is literally the witness for what the decompile should produce.

Heuristic per function: count copies of the shared terminal (a `call`/`jmp draw_window` here) and
backward `jmp .Ln` into an earlier label. -O0 duplicated tail => N copies, forward jmps only.
-O2 merged tail => 1 copy, a backward jmp into it (the cross-jump / address inversion).

Usage:  py -3 tools/probe-opt-levels.py [corpus/07_terminal_merge.c] [--tail draw_window]
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
DEFAULT_GCC = r"C:\msys64\ucrt64\bin\gcc.exe"


def gcc_path():
    return os.environ.get("LA_GCC", DEFAULT_GCC)


def compile_asm(gcc, src, opt):
    out = os.path.join(BUILD, f"{os.path.splitext(os.path.basename(src))[0]}_{opt}.s")
    env = os.environ.copy()
    env["PATH"] = os.path.dirname(gcc) + os.pathsep + env.get("PATH", "")
    subprocess.run([gcc, opt, "-S", os.path.abspath(src), "-o", out],
                   check=True, capture_output=True, text=True, env=env)
    return out


def fn_segment(asm_path, fn):
    lines = open(asm_path, encoding="utf-8", errors="replace").read().splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == f"{fn}:"), None)
    if start is None:
        return []
    # End at the proc terminator (msys2/mingw emits .seh_endproc, not .cfi_endproc) or, failing
    # that, the next top-level function label — so one function's asm never bleeds into the next.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if ".seh_endproc" in lines[i] or ".cfi_endproc" in lines[i]:
            end = i
            break
        if re.match(r"^[A-Za-z_]\w*:\s*$", lines[i]):   # next function symbol
            end = i
            break
    return lines[start:end]


def analyze(seg, tail):
    labels = {}                    # label -> line index
    for i, l in enumerate(seg):
        m = re.match(r"^(\.L\d+):", l.strip())
        if m:
            labels[m.group(1)] = i
    tail_copies = sum(1 for l in seg if re.search(rf"(call|jmp)\s+{re.escape(tail)}\b", l))
    backward = []
    for i, l in enumerate(seg):
        m = re.match(r"^\s*jmp\s+(\.L\d+)", l)
        if m and m.group(1) in labels and labels[m.group(1)] < i:
            backward.append(m.group(1))
    return tail_copies, backward


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default=os.path.join(ROOT, "corpus", "07_terminal_merge.c"))
    ap.add_argument("--tail", default="draw_window", help="the shared-tail symbol to count copies of")
    ap.add_argument("--fns", default="family2_common_tail,family1_guard_merge")
    args = ap.parse_args()

    gcc = gcc_path()
    os.makedirs(BUILD, exist_ok=True)
    o0 = compile_asm(gcc, args.src, "-O0")
    o2 = compile_asm(gcc, args.src, "-O2")
    print(f"{os.path.basename(args.src)}  (tail symbol: {args.tail})\n")
    for fn in args.fns.split(","):
        c0, b0 = analyze(fn_segment(o0, fn), args.tail)
        c2, b2 = analyze(fn_segment(o2, fn), args.tail)
        print(f"  {fn}")
        print(f"    -O0: tail copies={c0}  backward-jmps={len(b0)}  "
              f"-> {'DUPLICATED (address-ordered)' if c0 > 1 and not b0 else 'single/other'}")
        print(f"    -O2: tail copies={c2}  backward-jmps={len(b2)} {b2}  "
              f"-> {'MERGED + CROSS-JUMP (address-inverted)' if b2 else 'single/other'}")
    print("\n  => a tail that is DUPLICATED at -O0 but MERGED-with-backward-jmp at -O2 is a "
          "cross-jump\n     OPTIMIZATION; the decompiler atom that recovers it INVERTS the "
          "optimization (the\n     -O0 form is the target). A tail single+address-ordered at BOTH "
          "is a base lowering.")


if __name__ == "__main__":
    sys.exit(main())
