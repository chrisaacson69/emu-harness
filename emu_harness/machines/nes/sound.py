"""The sound lane: run a title's own sound engine headless, record what it writes to the APU, play it.

Converted from Mappy's sound_capture.py (mappy-decompiler), which checked its stream against Mesen write
for write before anything was synthesized. Nothing here decodes a title's music format: the ROM's own
engine turns its byte streams into APU writes, so the only title-specific part is how the game drives
that engine. A title supplies it as an engine adapter:

    class Engine:
        events = {"name": (request, hold[, "loop"])}
            # request: what the game passes to start it (any value)
            # hold: frames the game keeps requesting it, or "loop" = requested for good (Mappy's
            #       round music: the engine stops a voice whose trigger is not renewed)
            # "loop": the stream repeats by itself after `hold` (NA1's songs, requested once)
        def machine(self):    -> the bus the engine's CPU writes through (fresh machine)
        def init(self):       the game's sound init (frame 0)
        def request(self, r): what the game does to start sound `r` (called on each held frame)
        def tick(self):       one frame of the engine (what the NMI or main loop calls)

and `capture` / `render_event` / the CLI do the rest:

    py -3.14 -m emu_harness.machines.nes.sound <adapter.py> [--list] [--wav DIR] [--json OUT]

The APU writes are recorded by tapping the bus's `write` (any bus whose CPU calls `bus.write`:
machines.nes.bus.Bus, the KOEI VM's Memory). $4014 (OAM DMA) and $4016 (controller strobe) are not APU.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

CHANNELS = {0x4000: "pulse1", 0x4004: "pulse2", 0x4008: "triangle", 0x400C: "noise", 0x4010: "dmc"}
NOT_APU = (0x4014, 0x4016)
LEAD, TAIL_S = 0.010, 0.3       # WAV trim: seconds kept before the first and after the last sound
CAP = 4000                      # frames for a one-shot event (Mappy's longest, the bonus music, is 2,446)


class Recorder:
    """The APU stream: [(frame, addr, value)], frame set by the capture loop."""
    def __init__(self):
        self.frame = 0
        self.writes = []


def tap(bus, rec):
    """Record every APU register write `bus` takes, stamped with rec.frame. -> bus."""
    inner = bus.write

    def write(addr, val):
        a = addr & 0xFFFF
        if 0x4000 <= a <= 0x4017 and a not in NOT_APU:
            rec.writes.append((rec.frame, a, val & 0xFF))
        inner(addr, val)
    bus.write = write
    return bus


def capture(engine, request, frames, hold=1):
    """-> [(frame, addr, value)]: frame 0 is the init, frames 1.. one tick each; `request` is made
    before each of the first `hold` ticks, as the game does for a held sound."""
    rec = Recorder()
    tap(engine.machine(), rec)
    engine.init()
    for f in range(1, frames + 1):
        if f <= hold:
            engine.request(request)
        rec.frame = f
        engine.tick()
    return rec.writes


def find_loop(stream, max_start=1500, max_period=4000):
    """-> (start, period) of the first frame range that repeats exactly (every APU write, in order), or None."""
    n = max(f for f, _, _ in stream)
    seq = [[] for _ in range(n + 1)]
    for f, a, v in stream:
        if f:
            seq[f].append((a, v))
    for period in range(120, max_period):
        for start in range(1, min(max_start, n - 2 * period)):
            if all(seq[f] == seq[f + period] for f in range(start, start + period)):
                return start, period
    return None


def channel(addr):
    return CHANNELS.get(addr & ~3, "control") if addr < 0x4014 else "control"


def last_change(stream):
    """The last frame on which any APU register changed value."""
    img, last = {}, 0
    for f, a, v in stream:
        if f and img.get(a) != v:
            img[a], last = v, f
    return last


def render_event(engine, request, hold, path, cap=CAP, loop=False):
    """Capture one event and write it as a WAV, trimmed to where it sounds. A looping event (hold "loop",
    or loop=True) is rendered as two of its loops. -> (seconds, gain, note)."""
    import numpy as np
    from emu_harness.machines.nes.apu import APU, CPU_HZ, FRAME_CYCLES, RATE, frames_to_cycles, write_wav
    if hold == "loop" or loop:
        stream = capture(engine, request, 8000, hold=8000 if hold == "loop" else hold)
        found = find_loop(stream)
        if found is None:
            raise ValueError("a looping event, but no loop within 8,000 frames")
        start, period = found
        frames = start + 2 * period
        note = f"loop of {period} frames ({period * FRAME_CYCLES / CPU_HZ:.2f} s) from frame {start}, x2"
    else:
        stream = capture(engine, request, cap, hold=hold)
        frames, note = min(last_change(stream) + 60, cap), f"held {hold} frame(s)"
    stream = [w for w in stream if w[0] <= frames]
    x = APU().render(frames_to_cycles(stream), frames * FRAME_CYCLES / CPU_HZ)
    live = np.flatnonzero(np.abs(np.diff(x)) > 1e-6)                  # the samples where anything sounds
    if not len(live):
        return 0.0, 0.0, note + ", silent"
    a = max(0, live[0] - int(LEAD * RATE))
    b = len(x) if hold == "loop" or loop else min(len(x), live[-1] + int(TAIL_S * RATE))
    gain = write_wav(x[a:b], path, peak=0.89)                          # -1 dBFS
    return (b - a) / RATE, gain, note


def load_adapter(path):
    """Import a title's adapter file; -> its Engine()."""
    path = Path(path).resolve()
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Engine()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="emu_harness.machines.nes.sound", description=__doc__.split("\n")[0])
    ap.add_argument("adapter", type=Path, help="the title's engine adapter (a .py with class Engine)")
    ap.add_argument("--list", action="store_true", help="per-event summary (default)")
    ap.add_argument("--frames", type=int, default=3600, help="frames per --list / --json capture")
    ap.add_argument("--wav", type=Path, help="write one WAV per event into this folder")
    ap.add_argument("--json", type=Path, help="write the streams: {event: [[frame, addr, value], ...]}")
    a = ap.parse_args(argv)
    engine = load_adapter(a.adapter)
    if a.wav:
        a.wav.mkdir(parents=True, exist_ok=True)
        for name, (req, hold, *flags) in engine.events.items():
            secs, gain, note = render_event(engine, req, hold, a.wav / f"{name}.wav", loop="loop" in flags)
            print(f"{name:18s} {str(req):13s} {secs:5.2f} s  gain x{gain / 0.9:4.1f}  {note}")
        return 0
    streams = {}
    print(f"{'event':>18}  {'writes':>6}  {'first':>5}  {'last':>5}  loops  channels")
    for name, (req, *_) in engine.events.items():
        w = [x for x in capture(engine, req, a.frames) if x[0] > 0]   # frame 0 is the shared init
        streams[name] = w
        if not w:
            print(f"{name:>18}  {0:6d}      -      -    -    (silent)")
            continue
        last = w[-1][0]
        chans = sorted({channel(x[1]) for x in w})
        print(f"{name:>18}  {len(w):6d}  {w[0][0]:5d}  {last:5d}  {'yes' if last > a.frames - 600 else ' no'}"
              f"    {', '.join(chans)}")
    if a.json:
        a.json.write_text(json.dumps({k: [list(x) for x in s] for k, s in streams.items()}), encoding="utf-8")
        print("wrote", a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
