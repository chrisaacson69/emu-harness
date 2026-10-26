"""Battery saves are never lost to a run (row O3 hazard, fixed in O4/P, 2026-10-26)."""
from pathlib import Path

from emu_harness.backends.mesen import Mesen
from emu_harness.machines.nes import BACKUP_DIR, NES, wipe_battery


def test_wipe_battery_keeps_a_backup(tmp_path):
    saves = tmp_path / "Saves"
    saves.mkdir()
    (saves / "Game [!].sav").write_bytes(b"\x01\x02")
    (saves / "Other.sav").write_bytes(b"\x09")
    moved = wipe_battery(Path("roms/Game [!].nes"), {"home": tmp_path})
    assert not (saves / "Game [!].sav").exists()
    assert (saves / "Other.sav").exists()
    assert len(moved) == 1 and moved[0].parent == saves / BACKUP_DIR
    assert moved[0].read_bytes() == b"\x01\x02"
    (saves / "Game [!].sav").write_bytes(b"\x01\x02")      # the same save again: no second copy
    assert wipe_battery(Path("Game [!].nes"), {"home": tmp_path}) == moved
    assert not (saves / "Game [!].sav").exists()
    assert len(list((saves / BACKUP_DIR).iterdir())) == 1


def _fake_mesen(monkeypatch, run_writes: bytes):
    """Mesen without the emulator: close() writes the run's battery, as Mesen does on exit."""
    def init(self, rom, **kw):
        self.rom, self.cfg = Path(rom), kw["config"]
        self.seen = (self.cfg["home"] / "Saves" / f"{self.rom.stem}.sav")
        self.seen = self.seen.read_bytes() if self.seen.exists() else None

    def close(self):
        (self.cfg["home"] / "Saves" / f"{self.rom.stem}.sav").write_bytes(run_writes)
        return 0
    monkeypatch.setattr(Mesen, "__init__", init)
    monkeypatch.setattr(Mesen, "close", close)


def test_nes_runs_clean_and_puts_the_save_back(tmp_path, monkeypatch):
    _fake_mesen(monkeypatch, b"run")
    (tmp_path / "Saves").mkdir()
    sav = tmp_path / "Saves" / "Game.sav"
    sav.write_bytes(b"mine")
    m = NES(Path("Game.nes"), config={"home": tmp_path})
    assert m.seen is None                                  # the run powered on with no save
    m.close()
    assert sav.read_bytes() == b"mine"


def test_nes_runs_on_a_given_sram(tmp_path, monkeypatch):
    _fake_mesen(monkeypatch, b"run")
    (tmp_path / "Saves").mkdir()
    m = NES(Path("Game.nes"), sram=b"dump", config={"home": tmp_path})
    assert m.seen == b"dump"
    m.close()
    assert not (tmp_path / "Saves" / "Game.sav").exists()  # there was none before the run
