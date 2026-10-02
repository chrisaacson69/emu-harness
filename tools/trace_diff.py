"""Compare two per-frame RAM traces (record = 1 flag byte + 2048 bytes RAM) and find where
they first diverge. Format written by tools/bizhawk/trace_bk2.lua and the mesen-harness
bridge `trace` command (replay_log.py --trace).

  py -3.14 tools/trace_diff.py <reference.bin> <candidate.bin> [--shift auto|N]
         [--ignore 0x100-0x1FF ...] [--show 12]

--shift N: candidate record j+N is compared with reference record j. `auto` picks the
shift (-4..4) with the fewest differing bytes over the first 600 records.
Addresses that differ in every one of the first 600 aligned records are reported and
ignored as baseline noise (power-on garbage, emulator-private bytes) unless --strict.
"""
import argparse
import sys
from pathlib import Path

REC = 1 + 0x800


def load(path: Path) -> list[bytes]:
    data = path.read_bytes()
    n = len(data) // REC
    return [data[i * REC:(i + 1) * REC] for i in range(n)]


def parse_ranges(specs):
    out = set()
    for s in specs:
        lo, _, hi = s.partition("-")
        out.update(range(int(lo, 0), int(hi or lo, 0) + 1))
    return out


def ndiff(a: bytes, b: bytes) -> int:
    return sum(x != y for x, y in zip(a[1:], b[1:]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ref", type=Path)
    ap.add_argument("cand", type=Path)
    ap.add_argument("--shift", default="auto")
    ap.add_argument("--ignore", nargs="*", default=[])
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--show", type=int, default=16)
    ap.add_argument("--from-sync", action="store_true",
                    help="skip the boot phase: start at the first record whose RAM matches exactly, "
                         "and list every later divergent run of records instead of stopping at the first")
    ap.add_argument("--ref-flag", choices=["lag", "read"], default="lag",
                    help="meaning of the reference flag byte (BizHawk trace_bk2.lua writes lag)")
    ap.add_argument("--cand-flag", choices=["lag", "read"], default="read",
                    help="meaning of the candidate flag byte (the Mesen bridge writes input-read)")
    a = ap.parse_args()

    ref, cand = load(a.ref), load(a.cand)
    print(f"reference {len(ref)} records, candidate {len(cand)} records")

    window = 600
    if a.shift == "auto":
        scores = {}
        for s in range(-4, 5):
            pairs = [(ref[j], cand[j + s]) for j in range(window) if 0 <= j + s < len(cand) and j < len(ref)]
            scores[s] = sum(ndiff(x, y) for x, y in pairs) / max(len(pairs), 1)
        for s, v in sorted(scores.items()):
            print(f"  shift {s:+d}: {v:8.1f} differing bytes/frame")
        shift = min(scores, key=scores.get)
    else:
        shift = int(a.shift)
    print(f"using shift {shift:+d} (candidate record j{shift:+d} vs reference record j)")

    n = min(len(ref), len(cand) - shift) if shift >= 0 else min(len(ref) + shift, len(cand))
    lo = max(0, -shift)
    ignore = parse_ranges(a.ignore)
    if not a.strict:
        always = set(range(0x800))
        for j in range(lo, lo + window):
            r, c = ref[j], cand[j + shift]
            always &= {i for i in range(0x800) if r[1 + i] != c[1 + i]}
        if always:
            print(f"baseline: {len(always)} addresses differ in all of the first {window} frames, "
                  f"ignored: {', '.join(f'${x:03X}' for x in sorted(always)[:40])}"
                  + (" ..." if len(always) > 40 else ""))
        ignore |= always

    if a.from_sync:
        return sync_report(ref, cand, shift, lo, n, ignore, a)

    def lagged(rec, kind):
        return rec[0] == 1 if kind == "lag" else rec[0] == 0

    lag_mismatch = None
    for j in range(lo, n):
        r, c = ref[j], cand[j + shift]
        if lag_mismatch is None and lagged(r, a.ref_flag) != lagged(c, a.cand_flag):
            lag_mismatch = j
        diffs = [i for i in range(0x800) if i not in ignore and r[1 + i] != c[1 + i]]
        if diffs:
            print(f"\nFIRST DIVERGENCE at reference record {j} (candidate record {j + shift}); "
                  f"{len(diffs)} bytes differ")
            print(f"  lagged: ref={lagged(r, a.ref_flag)} cand={lagged(c, a.cand_flag)}")
            for i in diffs[:a.show]:
                hist = " ".join(f"{ref[k][1 + i]:02x}/{cand[k + shift][1 + i]:02x}"
                                for k in range(max(lo, j - 3), j + 1))
                print(f"  ${i:03X}: ref {r[1 + i]:3d}  cand {c[1 + i]:3d}   last 4 (ref/cand): {hist}")
            if lag_mismatch is not None:
                print(f"  first lag-flag mismatch at reference record {lag_mismatch}")
            return 1
    print(f"\nno divergence in {n - lo} compared records"
          + (f"; first lag-flag mismatch at {lag_mismatch}" if lag_mismatch is not None else ""))
    return 0


def sync_report(ref, cand, shift, lo, n, ignore, a) -> int:
    same = lambda j: all(ref[j][1 + i] == cand[j + shift][1 + i] for i in range(0x800) if i not in ignore)
    start = next((j for j in range(lo, n) if ref[j][1:] == cand[j + shift][1:]), None)
    if start is None:
        print("never in sync: no record matches exactly")
        return 1
    print(f"in sync from reference record {start}")
    runs, j = [], start
    while j < n:
        if ref[j][1:] != cand[j + shift][1:]:
            k = j
            while k < n and ref[k][1:] != cand[k + shift][1:]:
                k += 1
            addrs = sorted({i for m in range(j, min(k, j + 50)) for i in range(0x800)
                            if ref[m][1 + i] != cand[m + shift][1 + i]})
            runs.append((j, k, addrs))
            j = k
        else:
            j += 1
    for s, e, addrs in runs[:a.show]:
        tail = "" if e < n else "  (never resyncs)"
        print(f"  records {s}..{e - 1} ({e - s} frames) differ at "
              f"{', '.join(f'${x:03X}' for x in addrs[:12])}{' ...' if len(addrs) > 12 else ''}{tail}")
    print(f"{len(runs)} divergent stretches after sync; compared {n - start} records")
    return 1 if runs and runs[-1][1] >= n else 0


if __name__ == "__main__":
    sys.exit(main())
