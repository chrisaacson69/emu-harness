"""machines.nes.wipe_battery moves a save aside instead of deleting it (row O3 hazard, 2026-10-26)."""
from pathlib import Path

from emu_harness.machines.nes import BACKUP_DIR, wipe_battery


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
