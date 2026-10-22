"""audit_family.py - inventory the console-RE repo family for the migration ledger (MIGRATION.md).

Every .py file in the sibling repos: size, docstring, imports, sys.path hacks, cross-repo imports,
and which function bodies are copied verbatim across repos. Deterministic. Repos are found by the
sibling convention (../<name>); a missing repo stops the run rather than shrinking the inventory.

    py -3.14 tools/audit_family.py _scratch/audit.json

Verbatim copies are only half the duplication: the same job written independently shares no text,
so the role table in MIGRATION.md is built by reading, and folds are proven by comparing outputs.
"""
import ast
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # the folder the sibling repos live in
LIBS = ["emu-harness", "nes-render", "koei-nes", "koei-snes", "snes-decompiler", "game-annotation"]
TITLES = ["na1-decompiler", "ro3k-decompiler", "gk-decompiler", "na2-decompiler",
          "rot3k2-decompiler", "bk-decompiler", "GemFire-decompiler", "LEmp-decompiler",
          "Elite-decompiler", "UW-decompile", "rot3k2-snes-decompiler",
          "Gemfire-snes-decompiler", "AeroBiz-decompiler", "na1-snes-decompiler"]
SKIP = {".git", "__pycache__", "_scratch", "node_modules", ".venv", "venv"}
MIN_NODES = 40  # a function body smaller than this is too small to call a copy


def py_files(repo):
    for p in (ROOT / repo).rglob("*.py"):
        if not SKIP.intersection(p.parts):
            yield p


def body_hash(fn):
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # ignore the docstring
    if sum(1 for _ in ast.walk(ast.Module(body=body, type_ignores=[]))) < MIN_NODES:
        return None
    return hashlib.sha1("".join(ast.dump(s) for s in body).encode()).hexdigest()[:12]


def scan(repo, p):
    src = p.read_text(encoding="utf-8", errors="replace")
    r = {"repo": repo, "path": p.relative_to(ROOT / repo).as_posix(),
         "lines": src.count("\n") + 1, "kind": "lib" if repo in LIBS else "title",
         "imports": [], "defs": [], "hashes": {}, "doc": "", "cli": "__main__" in src,
         "syspath": re.findall(r"sys\.path\.(?:insert|append)\(([^\n]*)\)", src),
         "parse_error": None}
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        r["parse_error"] = str(e)
        return r
    doc = ast.get_docstring(tree)
    r["doc"] = doc.strip().splitlines()[0][:120] if doc and doc.strip() else ""
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            r["imports"] += [a.name.split(".")[0] for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            r["imports"].append(n.module.split(".")[0])
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            r["defs"].append(n.name)
            if isinstance(n, ast.FunctionDef) and (h := body_hash(n)):
                r["hashes"][n.name] = h
    r["imports"] = sorted(set(r["imports"]))
    return r


def main(out):
    missing = [r for r in LIBS + TITLES if not (ROOT / r).is_dir()]
    if missing:
        sys.exit(f"missing sibling repos: {missing}")
    recs, stems = [], defaultdict(set)
    for repo in LIBS + TITLES:
        for p in py_files(repo):
            recs.append(scan(repo, p))
            stems[p.stem].add(repo)

    # A cross-repo import is a module name the importer does not define itself.
    for r in recs:
        r["cross"] = sorted({f"{m}@{o}" for m in r["imports"] if r["repo"] not in stems.get(m, ())
                             for o in stems.get(m, ())})

    dups = defaultdict(list)
    for r in recs:
        for fn, h in r["hashes"].items():
            dups[h].append(f'{r["repo"]}/{r["path"]}::{fn}')
    dups = {h: v for h, v in dups.items() if len({x.split("/")[0] for x in v}) > 1}

    Path(out).write_text(json.dumps({"files": recs, "dup_bodies": dups}, indent=1), encoding="utf-8")

    by_repo = defaultdict(lambda: [0, 0])
    for r in recs:
        by_repo[r["repo"]][0] += 1
        by_repo[r["repo"]][1] += r["lines"]
    print(f'{"repo":28} files   lines')
    for k, (f, n) in by_repo.items():
        print(f"{k:28} {f:5} {n:7}")
    print(f"\nfiles: {len(recs)}  lines: {sum(r['lines'] for r in recs)}  "
          f"parse errors: {sum(1 for r in recs if r['parse_error'])}  "
          f"verbatim cross-repo function copies: {len(dups)}")
    edges = defaultdict(int)
    for r in recs:
        for c in r["cross"]:
            edges[(r["repo"], c.split("@")[1])] += 1
    print("\ncross-repo imports (importer -> provider: files)")
    for (a, b), n in sorted(edges.items(), key=lambda x: -x[1]):
        print(f"  {a} -> {b}: {n}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "_scratch/audit.json")
