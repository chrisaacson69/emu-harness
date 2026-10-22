"""mlb_compare.py - compare two Mesen .mlb files AS MESEN LOADS THEM, and name every difference.

    py -3.14 tools/mlb_compare.py <old.mlb> <new.mlb>

Bytes are the wrong oracle for label files: prefix dialect (P: vs NesPrgRom:), hex padding and a
trailing colon change bytes but not what the debugger shows. This loads each file with the rules
of Mesen 2's CodeLabel.FromString + LabelManager.SetLabel (legacy prefixes, split on the first
three colons, "\\n" in comments, LabelRegex, last label at a key wins) and compares the result.
Exit 1 if any difference falls outside the named categories. Used as the row A oracle (MIGRATION.md).
"""
import re
import sys
from collections import Counter
from pathlib import Path

LEGACY = {"G": "NesMemory", "R": "NesInternalRam", "P": "NesPrgRom", "S": "NesSaveRam", "W": "NesWorkRam"}
LABEL = re.compile(r"^[@_a-zA-Z]+[@_a-zA-Z0-9]*$")
EXPLAINED = {
    "comment: whitespace/newlines only",
    "comment: ':' restored (old tool wrote ';')",
    "comment: old one was truncated",
}


def mesen_load(path):
    """-> ({(memory type, address): (label, comment)}, number of lines Mesen rejects)."""
    labels, rejected = {}, 0
    for row in Path(path).read_text(encoding="utf-8").split("\n"):
        if not row:
            continue
        p = row.split(":", 3)
        if len(p) < 3:
            rejected += 1
            continue
        t = LEGACY.get(p[0], p[0] if len(p[0]) > 1 else None)
        if t is None or (p[2] and not LABEL.match(p[2])):
            rejected += 1
            continue
        labels[(t, int(p[1], 16))] = (p[2], p[3].replace("\\n", "\n") if len(p) > 3 else "")
    return labels, rejected


def _ws(s):
    return " ".join(s.split())


def classify(old, new):
    (on, oc), (nn, nc) = old, new
    if on != nn:
        return "name differs"
    if oc == nc:
        return None
    if _ws(oc) == _ws(nc):
        return "comment: whitespace/newlines only"
    if _ws(oc) == _ws(nc.replace(":", ";")):
        return "comment: ':' restored (old tool wrote ';')"
    if _ws(nc).startswith(_ws(oc)) or _ws(nc.replace(":", ";")).startswith(_ws(oc)):
        return "comment: old one was truncated"
    return "comment differs"


def compare(old_path, new_path):
    old, old_rej = mesen_load(old_path)
    new, new_rej = mesen_load(new_path)
    cats, examples = Counter(), {}
    for k in old.keys() | new.keys():
        c = ("only in old" if k not in new else "only in new" if k not in old
             else classify(old[k], new[k]))
        if c:
            cats[c] += 1
            examples.setdefault(c, (k, old.get(k), new.get(k)))
    same = sum(1 for k in old.keys() & new.keys() if old[k] == new[k])
    return old, new, old_rej, new_rej, same, cats, examples


def main(argv):
    old, new, orj, nrj, same, cats, ex = compare(argv[1], argv[2])
    print(f"Mesen loads old {len(old)} (+{orj} rejected) / new {len(new)} (+{nrj} rejected); identical {same}")
    for c, n in cats.most_common():
        (t, a), o, nw = ex[c]
        print(f"  {n:5}  {c}" + ("" if c in EXPLAINED else "   <- review"))
        print(f"         e.g. {t}:{a:04X}  old={str(o)[:80]}  new={str(nw)[:80]}")
    return 1 if set(cats) - EXPLAINED else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
