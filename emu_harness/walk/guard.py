"""The regen guard of a naming walk (row J of MIGRATION.md): after a batch of names is written and the
source regenerated, the source may change only by those names.

Two checks, one per walk family, both over a snapshot taken BEFORE regen (not git HEAD, so a dirty
tree cannot false-positive):

  identifier collapse   na1-decompiler tools/walk_guard.py (label-walk + var-walk apply): every
                        identifier on each non-comment line becomes ID, far-frame slot forms included,
                        and the line SEQUENCE must not change. Catches any logic / structure change and
                        a name leaking into a file outside the allowed set (cross-bank bleed).
  name mapping          AeroBiz-decompiler tools/walk_apply.py: every routine name, old and new, is
                        mapped back to sub_<ADDR> and the texts must then be EQUAL. Stricter: a rename
                        to the wrong routine's name fails it too.
"""
import re
from pathlib import Path


# ---- identifier collapse (na1 walk_guard, verbatim) -----------------------------------------
def snapshot_decompiled(decompiled_dir, pattern="*.c"):
    """{filename: text} for every file matching `pattern` in the directory. Take this BEFORE regen."""
    return {p.name: p.read_text(encoding="utf-8") for p in Path(decompiled_dir).glob(pattern)}


# Far-frame lvalue forms the decompiler renders for un-named spill locals. var-walk substitutes the
# WHOLE expression with a name (deref -> `name`, address-of -> `&name`), so to keep "pure rename"
# honest the raw form is identifier-equivalent: derefs collapse to `ID`, the address-of to `&ID`,
# BEFORE the generic identifier collapse (derefs first, so the inner `(fp - N)` of a deref is not
# eaten by the address-of rule). Symmetric on before/after; a no-op on label-walk diffs.
_FAR_DEREF = re.compile(r'\*\((?:word|byte)\*\)\(fp [+-] \d+\)')
_FAR_ADDR = re.compile(r'\(fp [+-] \d+\)')


def norm_lines(text):
    """Non-comment lines with every identifier collapsed to 'ID' (order preserved)."""
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("//"):
            continue
        s = _FAR_DEREF.sub("ID", s)
        s = _FAR_ADDR.sub("&ID", s)
        out.append(re.sub(r"[A-Za-z_]\w*", "ID", s))
    return out


def find_guard_violations(before, after, allowed):
    """-> (others, nonrename): changed files outside `allowed` (cross-bank bleed), and allowed files
    whose change is not a pure rename. Both empty == clean."""
    allowed = set(allowed)
    changed = {n for n in set(before) | set(after) if before.get(n) != after.get(n)}
    others = sorted(changed - allowed)
    nonrename = [n for n in sorted(changed & allowed)
                 if norm_lines(before.get(n, "")) != norm_lines(after.get(n, ""))]
    return others, nonrename


# ---- name mapping (AeroBiz walk_apply) ------------------------------------------------------
def normalize_names(text, name2tok, is_listing):
    """Map routine names to their sub_<ADDR> token; in a listing also drop annotations that are only
    a routine token and the 'N symbols visible' count (it grows with every name)."""
    if name2tok:
        rx = re.compile(r"\b(" + "|".join(sorted(map(re.escape, name2tok), key=len, reverse=True)) + r")\b")
        text = rx.sub(lambda m: name2tok[m.group(1)], text)
    if is_listing:
        text = re.sub(r"[ \t]+; sub_[0-9A-F]{4}[ \t]*$", "", text, flags=re.M)   # [ \t], not \s: keep newlines
        text = re.sub(r"\b\d+ symbols visible\b", "N symbols visible", text)
        text = re.sub(r"[ \t]+$", "", text, flags=re.M)
    return text


def name_map_violations(before, after, name2tok, is_listing=lambda fn: fn.endswith(".src")):
    """-> [(file, line no, old, new)] for every changed file whose texts differ once names are mapped
    to tokens (the first differing line of each). Empty == the diff is pure renames."""
    bad = []
    for fn in sorted(set(before) | set(after)):
        old, new = before.get(fn, ""), after.get(fn, "")
        if old == new:
            continue
        ol = normalize_names(old, name2tok, is_listing(fn)).splitlines()
        nl = normalize_names(new, name2tok, is_listing(fn)).splitlines()
        if ol != nl:
            i = next((k for k in range(min(len(ol), len(nl))) if ol[k] != nl[k]), min(len(ol), len(nl)))
            bad.append((fn, i + 1, ol[i] if i < len(ol) else "<eof>", nl[i] if i < len(nl) else "<eof>"))
    return bad
