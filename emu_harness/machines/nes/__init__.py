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
    """A Mesen instance with the NES pad map. The run's battery is clean by default, or `sram`;
    either way the save Mesen held for this ROM is put back, byte for byte, when the run closes
    (and a copy stays in Saves/emu-harness-backup/, see wipe_battery). clean_battery=False runs on
    whatever save is there and leaves the result in place."""

    def __init__(self, rom: Path, *, clean_battery: bool = True, sram: bytes | None = None,
                 ram_power_on: str | None = None, **kw):
        """sram: the battery save for this run (e.g. an SRAM dump). ram_power_on: AllZeros |
        AllOnes | Random (Mesen's RamState). BizHawk's quickerNES powers on with RAM all 0xFF, so
        replaying its movies needs AllOnes."""
        self._saves = None
        if clean_battery or sram is not None:
            cfg = kw.get("config") or load_config()
            self._saves = (cfg["home"], {p: p.read_bytes() for p in save_files(cfg["home"], Path(rom))})
            wipe_battery(rom, cfg)
            if sram is not None:
                (cfg["home"] / "Saves" / f"{Path(rom).stem}.sav").write_bytes(sram)
        if ram_power_on:
            kw["switches"] = [*kw.get("switches", ()), f"--Nes.RamPowerOnState={ram_power_on}"]
        try:
            super().__init__(rom, keys=KEYS, **kw)
        except BaseException:
            self._restore_saves(Path(rom))
            raise

    def close(self) -> int | None:
        code = super().close()           # Mesen writes the run's battery on exit
        self._restore_saves(self.rom)
        return code

    def _restore_saves(self, rom: Path) -> None:
        if self._saves is None:
            return
        home, before = self._saves
        self._saves = None
        for p in save_files(home, rom):
            p.unlink()
        for p, data in before.items():
            p.write_bytes(data)

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
