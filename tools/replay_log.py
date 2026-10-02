"""Replay an NES input log (one line per frame from power-on) in Mesen, headless.

Prints watched RAM bytes every --every frames so a desync can be located, and the
final state plus a SHA-1 of system RAM ($0000-$07FF).

  py -3.14 tools/replay_log.py <rom> <inputs.txt> [--frames N] [--every 600]
         [--watch name=0xADDR ...] [--offset K]

--offset K drops (K>0) or prepends K blank frames (K<0) to test frame alignment.
Against BizHawk movies, --offset -1 aligns (verified on AIBeatsZelda run 6, 2026-10-02).
"""
import argparse
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from emu_harness.machines.nes import NES, read_input_log


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rom", type=Path)
    ap.add_argument("log", type=Path)
    ap.add_argument("--frames", type=int, default=0, help="stop after N log frames (0 = all)")
    ap.add_argument("--every", type=int, default=600)
    ap.add_argument("--watch", nargs="*", default=[], help="name=0xADDR")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--ram", choices=["AllZeros", "AllOnes", "Random"],
                    help="Mesen power-on RAM fill (BizHawk quickerNES = AllOnes)")
    ap.add_argument("--trace", type=Path, help="write a per-frame trace: 1 byte $4016-read flag "
                    "(0 = lag frame) + 2048 bytes of system RAM, at every endFrame")
    a = ap.parse_args()

    frames = read_input_log(a.log)
    if a.offset > 0:
        frames = frames[a.offset:]
    elif a.offset < 0:
        frames = [[]] * (-a.offset) + frames
    if a.frames:
        frames = frames[:a.frames]
    watch = [(n, int(v, 0)) for n, v in (w.split("=") for w in a.watch)]

    t0 = time.perf_counter()
    with NES(a.rom, ram_power_on=a.ram) as m:
        masks = [m.mask_from_names(f) for f in frames]
        if a.trace:
            m.trace("nesDebug", 0, 0x800, a.trace.resolve(), flag_addr=0x4016)

        def show(done):
            vals = " ".join(f"{n}={m.ram(addr)[0]}" for n, addr in watch)
            print(f"log {done:>7}  frame {m.frame():>7}  {vals}", flush=True)

        for i in range(0, len(masks), a.every):
            m.run_masks(masks[i:i + a.every])
            show(min(i + a.every, len(masks)))
        ram = m.ram(0, 0x800)
        print(f"replayed {len(masks)} frames in {time.perf_counter() - t0:.1f}s; "
              f"ram sha1 {hashlib.sha1(ram).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
