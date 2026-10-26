"""NES machine layer (pad map, battery wipe, RAM reads), currently bound to the Mesen backend."""
from __future__ import annotations

import time
from pathlib import Path

# Known coupling: NES subclasses the Mesen backend, and wipe_battery knows Mesen's
# Saves/ folder. Split it when a second backend gets a Python client (see CLAUDE.md).
from ...backends.mesen import Mesen, glob_escape, load_config, save_files

# Standard pad names (as written in input logs) -> Mesen getInput() names.
# Verified against Mesen 2.2.1 getInput(0): a,b,down,left,right,select,start,up.
PAD = {"Up": "up", "Down": "down", "Left": "left", "Right": "right",
       "Select": "select", "Start": "start", "B": "b", "A": "a"}
KEYS = list(PAD.values())   # mask bit i = KEYS[i]


BACKUP_DIR = "emu-harness-backup"


def wipe_battery(rom: Path, config: dict | None = None) -> list[Path]:
    """Move Mesen's battery save for this ROM aside, so the run powers on clean. A stale .sav
    silently changes a replay. Moved, never deleted: the save may be someone's game (row O3 lost
    NA1's this way). Returns the backups, in Saves/emu-harness-backup/, stamped with the time. A save
    whose bytes are already backed up is not copied again (each run would add one otherwise)."""
    cfg = config or load_config()
    backups = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for p in save_files(cfg["home"], Path(rom)):
        folder = p.parent / BACKUP_DIR
        folder.mkdir(exist_ok=True)
        data = p.read_bytes()
        same = [b for b in folder.glob(glob_escape(p.name) + ".*") if b.read_bytes() == data]
        if same:
            p.unlink()
            backups.append(same[0])
        else:
            dest = folder / f"{p.name}.{stamp}"
            p.replace(dest)
            backups.append(dest)
    return backups


class NES(Mesen):
    """A Mesen instance with the NES pad map; power-on from a clean battery by default (any existing
    save is moved to Saves/emu-harness-backup/, see wipe_battery)."""

    def __init__(self, rom: Path, *, clean_battery: bool = True, ram_power_on: str | None = None, **kw):
        """ram_power_on: AllZeros | AllOnes | Random (Mesen's RamState). BizHawk's quickerNES
        powers on with RAM all 0xFF, so replaying its movies needs AllOnes."""
        if clean_battery:
            wipe_battery(rom, kw.get("config"))
        if ram_power_on:
            kw["switches"] = [*kw.get("switches", ()), f"--Nes.RamPowerOnState={ram_power_on}"]
        super().__init__(rom, keys=KEYS, **kw)

    def ram(self, addr: int, length: int = 1) -> bytes:
        """CPU address space without read side effects."""
        return self.read("nesDebug", addr, length)

    def mask_from_names(self, names) -> int:
        return self.mask(PAD[n] for n in names)


def write_input_log(path: Path, masks: list[int], header: str = "") -> None:
    """Inverse of read_input_log, from masks in KEYS bit order."""
    names = {v: k for k, v in PAD.items()}
    lines = [f"# {h}" for h in header.splitlines()]
    for m in masks:
        lines.append(",".join(names[k] for i, k in enumerate(KEYS) if m >> i & 1))
    Path(path).write_text("\n".join(lines) + "\n")


def read_input_log(path: Path) -> list[list[str]]:
    """One line per frame from power-on: a comma list of pad names, blank = no buttons.
    Lines starting with '#' are headers."""
    frames = []
    for line in Path(path).read_text().splitlines():
        if line.startswith("#"):
            continue
        frames.append([b for b in line.split(",") if b])
    return frames
