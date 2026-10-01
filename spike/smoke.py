"""Smoke test the bridge: handshake, stepping, RAM reads, savestate round-trip."""
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mesen_harness import Mesen

rom = Path(sys.argv[1])
with Mesen(rom, keys=["up", "down", "left", "right", "select", "start", "b", "a"]) as m:
    print("info", m.info())
    print("getInput keys", m.input_keys())
    print("after 10:", m.step((), 10))
    ram = lambda: hashlib.sha1(m.read("nesDebug", 0, 0x800)).hexdigest()[:12]
    st = m.save()
    print("saved at frame", int(st[:8], 16), "state hex len", len(st))
    t = time.perf_counter(); f = m.step(("start",), 120); dt = time.perf_counter() - t
    a = ram(); print(f"run 120 -> frame {f} ram {a} ({120/dt:.0f} fps)")
    print("load ->", m.load(st))
    f = m.step(("start",), 120); b = ram(); print(f"rerun 120 -> frame {f} ram {b}")
    print("DETERMINISTIC" if a == b else "MISMATCH")
print("exit code", m.proc.returncode)
