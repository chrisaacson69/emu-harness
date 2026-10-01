"""nes-harness: the NES layer over mesen-harness (pad map, battery wipe, RAM reads)."""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import mesen_harness
except ImportError:  # sibling checkout convention: ../mesen-harness
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mesen-harness"))
    import mesen_harness

from mesen_harness import Mesen, load_config, save_files

# Standard pad names (as written in input logs) -> Mesen getInput() names.
# Verified against Mesen 2.2.1 getInput(0): a,b,down,left,right,select,start,up.
PAD = {"Up": "up", "Down": "down", "Left": "left", "Right": "right",
       "Select": "select", "Start": "start", "B": "b", "A": "a"}
KEYS = list(PAD.values())   # mask bit i = KEYS[i]


def wipe_battery(rom: Path, config: dict | None = None) -> list[Path]:
    """Delete Mesen's battery save for this ROM. A stale .sav silently changes a replay."""
    cfg = config or load_config()
    gone = save_files(cfg["home"], Path(rom))
    for p in gone:
        p.unlink()
    return gone


class NES(Mesen):
    """A Mesen instance with the NES pad map; power-on from a wiped battery by default."""

    def __init__(self, rom: Path, *, clean_battery: bool = True, **kw):
        if clean_battery:
            wipe_battery(rom, kw.get("config"))
        super().__init__(rom, keys=KEYS, **kw)

    def ram(self, addr: int, length: int = 1) -> bytes:
        """CPU address space without read side effects."""
        return self.read("nesDebug", addr, length)

    def mask_from_names(self, names) -> int:
        return self.mask(PAD[n] for n in names)


def read_input_log(path: Path) -> list[list[str]]:
    """One line per frame from power-on: a comma list of pad names, blank = no buttons.
    Lines starting with '#' are headers."""
    frames = []
    for line in Path(path).read_text().splitlines():
        if line.startswith("#"):
            continue
        frames.append([b for b in line.split(",") if b])
    return frames
