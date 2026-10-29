#!/usr/bin/env python3
"""compile-dumps.py — compile every corpus/*.c through GCC -O0 and collect the
GIMPLE-CFG dump for each. The .cfg dump (explicit basic blocks + edges + loop
header/latch metadata) is the artifact cfg-signature.py reads.

Usage:  py -3 tools/compile-dumps.py [--gcc PATH] [file.c ...]
Env:    LA_GCC overrides the gcc path (default: msys2 ucrt64 install).

Deterministic: the GCC dump pass-number prefix (e.g. 016t) varies by version, so
we glob for the produced *.cfg and normalize it to build/<stem>.cfg.
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(ROOT, "corpus")
BUILD = os.path.join(ROOT, "build")
DEFAULT_GCC = r"C:\msys64\ucrt64\bin\gcc.exe"


def find_gcc(override):
    for cand in (override, os.environ.get("LA_GCC"), DEFAULT_GCC, "gcc"):
        if not cand:
            continue
        path = shutil.which(cand) or (cand if os.path.exists(cand) else None)
        if path:
            return path
    sys.exit("error: gcc not found. Set $env:LA_GCC or pass --gcc PATH.")


def gcc_env(gcc):
    """gcc.exe needs its sibling msys2 runtime DLLs on PATH to launch."""
    env = os.environ.copy()
    env["PATH"] = os.path.dirname(gcc) + os.pathsep + env.get("PATH", "")
    return env


def compile_one(gcc, src, env):
    """Compile one .c, leaving build/<stem>.cfg (and <stem>.s) in BUILD."""
    stem = os.path.splitext(os.path.basename(src))[0]
    # Clear any stale dumps for this stem so the glob below is unambiguous.
    for old in glob.glob(os.path.join(BUILD, stem + "*.cfg")):
        os.remove(old)
    # cwd=BUILD + -dumpbase keeps the dump local and predictably named.
    cmd = [gcc, "-O0", "-fdump-tree-cfg", "-S",
           "-dumpbase", stem, os.path.abspath(src), "-o", stem + ".s"]
    proc = subprocess.run(cmd, cwd=BUILD, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        print(f"  FAIL {stem}: {proc.stderr.strip()}", file=sys.stderr)
        return None
    produced = sorted(glob.glob(os.path.join(BUILD, stem + "*.cfg")))
    if not produced:
        print(f"  FAIL {stem}: no .cfg dump produced", file=sys.stderr)
        return None
    dest = os.path.join(BUILD, stem + ".cfg")
    if produced[0] != dest:
        shutil.move(produced[0], dest)
        for extra in produced[1:]:
            os.remove(extra)
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gcc", help="path to gcc")
    ap.add_argument("files", nargs="*", help="specific .c files (default: all corpus)")
    args = ap.parse_args()

    gcc = find_gcc(args.gcc)
    env = gcc_env(gcc)
    os.makedirs(BUILD, exist_ok=True)
    ver = subprocess.run([gcc, "--version"], capture_output=True, text=True, env=env).stdout.splitlines()
    print(f"gcc: {gcc}\n     {ver[0] if ver else '?'}\n")

    srcs = args.files or sorted(glob.glob(os.path.join(CORPUS, "*.c")))
    ok = 0
    for src in srcs:
        dest = compile_one(gcc, src, env)
        if dest:
            ok += 1
            print(f"  ok   {os.path.basename(src):24s} -> build/{os.path.basename(dest)}")
    print(f"\n{ok}/{len(srcs)} compiled. CFG dumps in build/.")
    return 0 if ok == len(srcs) else 1


if __name__ == "__main__":
    sys.exit(main())
