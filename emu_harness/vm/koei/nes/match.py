#!/usr/bin/env python3
"""
Cross-title bytecode matching for the NES KOEI titles (row E of MIGRATION.md).

Every sub of every code bank of every title in games.toml, decoded through the title's own bank
setup (titles.bank_view) and signed by vm/koei/match. Names come from each title's bank-scoped
labels, so a $8000-$BFFF address means the sub of THAT bank.

    py -3.14 -m emu_harness.vm.koei.nes.match family [--layer bytecode|native] [--titles na1,...]
        Twin clusters across titles (each sub joined to its best match >= --floor in every other
        title), each with its canonical name -- the earliest title's -- and every member named
        otherwise: RENAME (identical code), READ (a near twin), FILL (unnamed).
    py -3.14 -m emu_harness.vm.koei.nes.match pair <a_vm.asm> <b_vm.asm> --game a --oracle b
        ro3k / gk bc_match.py, kept for its output: tokens from listing TEXT, names flat (unscoped).
"""
import argparse
import difflib
import re
import sys
from pathlib import Path

from emu_harness.vm.koei import match as M
from emu_harness.vm.koei.nes import titles

ANON = re.compile(r"^(sub|bc|native|fn|func|loc|L)_[0-9A-Fa-f]{4,6}$")


def is_named(name):
    return bool(name) and not ANON.match(name)


def title_subs(name):
    """{(bank, stub): (signature, name or None)} for every bytecode sub of every code bank."""
    cfg = titles.game_cfg(name)
    out = {}
    for bank in cfg.get("code_banks", []):
        vm, VD, _cfg, base, size, stubs = titles.bank_view(name, bank)
        labels = titles.bank_scoped_labels(cfg, bank)
        names = {a: n for a, (n, _c) in labels.items()}
        bounds = stubs + [base + size]
        for i, stub in enumerate(stubs):
            instrs, _pc, _t = VD.walk_sub(vm.mem, stub + 5, bounds[i + 1], names)
            sig = M.signature([ins["bytes"] for ins in instrs])
            out[(bank, stub)] = (sig, names.get(stub) or names.get(stub + 5))
    return out


def native_title_subs(name):
    """{("fixed", addr): (signature, name or None)} for the fixed bank's native subs (native_floor)."""
    from emu_harness.core.symbols import names_by_address, read_registry
    from emu_harness.cpu.m6502.signature import signature
    from emu_harness.vm.koei.nes import native_floor as NF
    t = NF.Title(name)
    subs, _census = NF.native_subs(t)
    names = names_by_address(read_registry(t.cfg["_labels"]))
    return {("fixed", a): (signature(t.buf, a), names.get(a)) for a in subs}


def clusters(subs, order, floor, min_len):
    """Union every sub with its best match (>= floor) in each other title. -> [[(title, key)]]."""
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a in order:
        for b in order:
            if a == b:
                continue
            cands = {k: s for k, (s, _n) in subs[b].items() if len(s) >= min_len}
            exact = {}
            for k, s in cands.items():
                exact.setdefault(tuple(s), k)
            for k, (sig, _n) in subs[a].items():
                if len(sig) < min_len:
                    continue
                hit = exact.get(tuple(sig))
                if hit is None:
                    hit, sc = M.best_match(sig, cands, floor)
                    if hit is None or sc < floor:
                        continue
                parent[find((a, k))] = find((b, hit))
    groups = {}
    for x in list(parent):
        groups.setdefault(find(x), []).append(x)
    rank = {t: i for i, t in enumerate(order)}
    return sorted((sorted(g, key=lambda m: (rank[m[0]], m[1])) for g in groups.values() if len(g) > 1),
                  key=lambda g: (rank[g[0][0]], g[0][1]))


def family(order, floor, min_len, layer):
    get = native_title_subs if layer == "native" else title_subs
    subs = {t: get(t) for t in order}
    print("%s subs per title: %s" % (layer, ", ".join("%s %d" % (t, len(subs[t])) for t in order)))
    groups = clusters(subs, order, floor, min_len)
    fmt = lambda t, k: "%s %s$%04X" % (t, "" if k[0] == "fixed" else "b%02d " % k[0], k[1])
    renames = fills = reads = 0
    print("\n%d twin clusters (>= %.2f, >= %d tokens). Canonical = the earliest title's name (games.toml"
          " order = release order;\nkoei-nes docs/shared-library.md: NA1 is the established vocabulary)."
          % (len(groups), floor, min_len))
    print("  RENAME = identical code, other name | READ = near twin, other name (shared-but-evolved:"
          " cross-ref, don't blind-rename) | FILL = unnamed here")
    for g in groups:
        named = [m for m in g if is_named(subs[m[0]][m[1]][1])]
        if not named:
            continue
        canon = named[0]
        cname, csig = subs[canon[0]][canon[1]][1], subs[canon[0]][canon[1]][0]
        lines = []
        for m in g:
            sig, nm = subs[m[0]][m[1]]
            if m == canon or nm == cname:
                continue
            if not is_named(nm):
                tag = "FILL"; fills += 1
            elif sig == csig:
                tag = "RENAME"; renames += 1
            else:
                tag = "READ"; reads += 1
            lines.append("    %-6s %-26s %s" % (tag, fmt(*m), nm or "-"))
        if lines:
            print("\n  %s  (%s, %d tokens; %d titles)" % (cname, fmt(*canon), len(csig), len({m[0] for m in g})))
            print("\n".join(lines))
    print("\nTOTAL: %d clusters; %d RENAME, %d READ, %d FILL" % (len(groups), renames, reads, fills))


# ---- oracles: GemFire's multi_oracle.py, every oracle from its profile ----------------------
def oracles(title, onames, mode):
    """A title's fixed-bank native subs against several oracle titles at once: a sub that diverged
    from the first (primary) oracle may still be a generic helper another sibling kept unchanged."""
    from emu_harness.core.symbols import names_by_address, read_registry
    from emu_harness.cpu.m6502.signature import signature
    from emu_harness.vm.koei.nes import native_floor as NF
    t = NF.Title(title)
    subs, jt = NF.native_subs(t)
    built = []
    for on in onames:
        o = NF.Title(on)
        names = names_by_address(read_registry(o.cfg["_labels"]))
        sig = {}
        for a in names:
            if o.in_bank(a) and not o.is_stub(a):         # skip that title's own bytecode stubs
                s = signature(o.buf, a)
                if len(s) >= 3:
                    sig[a] = s
        built.append((on, names, sig))
    rows = []
    for a in subs:
        sig = signature(t.buf, a)
        if len(sig) < 3:
            continue
        per = []
        for tag, names, osig in built:
            oa, sc = None, 0.0
            for b, s in osig.items():
                r = difflib.SequenceMatcher(None, sig, s).ratio()
                if r > sc:
                    sc, oa = r, b
            per.append((tag, oa, sc, names.get(oa)))
        rows.append((a, jt.get(a, 0), per[0], max(per, key=lambda p: p[2]), per))
    P = onames[0]
    recovered = [r for r in rows if r[2][2] < 0.70 and r[3][2] >= 0.82]
    specific = [r for r in rows if max(p[2] for p in r[4]) < 0.70]
    show = {"recovered": recovered, "specific": specific}.get(mode, rows)
    if mode == "all":
        print(f"{'addr':>6} {'cN':>3}  {P:>6} | {'BEST oracle':>22} {'score':>5}")
        print("-" * 70)
        for a, cN, rot, gb, per in sorted(show, key=lambda r: (-r[3][2])):
            tag, oa, sc, nm = gb
            flag = "RECOVER" if (rot[2] < 0.70 and sc >= 0.82) else ("spec" if sc < 0.70 else "")
            print(f"${a:04X} {cN:>3}  {rot[2]:>6.2f} | {tag:>5} {str(nm or '-'):>16} {sc:>5.2f}  {flag}")
    else:
        head = (f"RECOVERABLE GENERICS ({P}<.70 but a sibling >=.82)" if mode == "recovered"
                else f"GENUINELY {title.upper()}-SPECIFIC (<.70 across ALL oracles)")
        print(f"=== {head}: {len(show)} ===")
        for a, cN, rot, gb, per in sorted(show, key=lambda r: (-r[3][2] if mode == "recovered" else -r[1])):
            tag, oa, sc, nm = gb
            allsc = " ".join(f"{t}:{s:.2f}" for t, _, s, _ in per)
            if mode == "recovered":
                print(f'${a:04X}  {cN}cal  -> {tag} {nm} (${oa:04X}) {sc:.2f}   [{allsc}]')
            else:
                print(f'${a:04X}  {cN}cal  best={tag} {nm or "-"} {sc:.2f}   [{allsc}]')
    print("-" * 70)
    print(f"total fingerprintable: {len(rows)} | recoverable-generic: {len(recovered)} | "
          f"{title}-specific: {len(specific)}")


# ---- pair: bc_match.py's listing-text matcher, kept for its output --------------------------
SUB = re.compile(r";\s*sub\s+\$([0-9A-Fa-f]+)")
MN = re.compile(r'^\s*\$[0-9A-Fa-f]+\s+[0-9A-Fa-f ]+\s+([A-Z][A-Z0-9_]+)(.*)$')
ROMADDR = re.compile(r'\$[89A-Fa-f][0-9A-Fa-f]{3}\b')


def text_sigs(path):
    """addr -> [token] per sub, from a listing's text (bc_match.sigs, verbatim)."""
    out, cur = {}, None
    for ln in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = SUB.search(ln)
        if m:
            cur = int(m.group(1), 16); out[cur] = []; continue
        if cur is None:
            continue
        mm = MN.match(ln)
        if mm:
            mn, opnd = mm.group(1), mm.group(2)
            opnd = opnd.split(';')[0]
            imm = re.search(r'(?:operand = |[+\-])(\d+)', opnd)
            imm = re.search(r'inline operand = (-?\d+)', mm.group(2)) or imm
            tok = mn
            if imm:
                tok += f"#{imm.group(1)}"
            elif ROMADDR.search(opnd):
                tok += "/A"
            elif re.search(r'\$[0-7][0-9A-Fa-f]{3}', opnd):
                tok += "/" + re.search(r'\$[0-7][0-9A-Fa-f]{3}', opnd).group(0)
            out[cur].append(tok)
    return out


def flat_names(toml_path):
    pat = re.compile(r'"0x([0-9A-Fa-f]+)"\s*=\s*\{\s*name\s*=\s*"([^"]+)"')
    return {int(m.group(1), 16): m.group(2) for m in
            pat.finditer(Path(toml_path).read_text(encoding="utf-8", errors="replace"))}


def pair(a_asm, b_asm, game, oracle):
    ro_sig, na_sig = text_sigs(a_asm), text_sigs(b_asm)
    ro_nm = flat_names(titles.game_cfg(game)["_labels"])
    na_nm = flat_names(titles.game_cfg(oracle)["_labels"])
    rows = []
    for ra, rs in ro_sig.items():
        best_a, best = None, 0.0
        for na, ns in na_sig.items():
            if abs(len(rs) - len(ns)) > max(len(rs), len(ns)) * 0.5 + 4:
                continue
            sc = difflib.SequenceMatcher(None, rs, ns).ratio()
            if sc > best:
                best, best_a = sc, na
        rows.append((ra, len(rs), best_a, best))
    rows.sort(key=lambda r: -r[3])
    shared = sum(1 for *_, s in rows if s >= 0.90)
    near = sum(1 for *_, s in rows if 0.70 <= s < 0.90)
    spec = sum(1 for *_, s in rows if s < 0.70)
    A, B = game.upper(), oracle.upper()
    print(f"{A} subs: {len(rows)} | {B} subs: {len(na_sig)}")
    print(f"  SHARED (>=.90): {shared}   NEAR (.70-.90): {near}   GAME-SPECIFIC (<.70): {spec}\n")
    print(f"{A:>6} {'len':>4} {B:>6} {'score':>6}  {A} name  <->  {B} name")
    print("-" * 100)
    for ra, ln, na, sc in rows:
        rn = ro_nm.get(ra, f"sub_{ra:04x}")
        nn = na_nm.get(na, f"sub_{na:04x}") if na is not None else "-"
        tag = "==" if sc >= 0.90 else ("~ " if sc >= 0.70 else "!!")
        nas = f"${na:04x}" if na is not None else "     -"
        print(f"${ra:04x} {ln:>4} {nas} {sc:>6.2f}  {tag} {rn}  <->  {nn}")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    f = sp.add_parser("family")
    f.add_argument("--titles", default=None, help="comma list (default: every title in games.toml)")
    f.add_argument("--floor", type=float, default=0.90)
    f.add_argument("--min-len", type=int, default=8, help="skip subs shorter than this (stubs, thunks)")
    f.add_argument("--layer", choices=("bytecode", "native"), default="bytecode")
    o = sp.add_parser("oracles")
    o.add_argument("title")
    o.add_argument("--oracles", required=True, help="comma list; the first is the primary oracle")
    o.add_argument("--mode", choices=("all", "recovered", "specific"), default="all")
    p = sp.add_parser("pair")
    p.add_argument("a_asm"); p.add_argument("b_asm")
    p.add_argument("--game", required=True); p.add_argument("--oracle", required=True)
    a = ap.parse_args(argv[1:])
    if a.cmd == "pair":
        pair(a.a_asm, a.b_asm, a.game, a.oracle)
    elif a.cmd == "oracles":
        oracles(a.title, a.oracles.split(","), a.mode)
    else:
        names = a.titles.split(",") if a.titles else list(titles.load_games())
        family(names, a.floor, a.min_len, a.layer)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
