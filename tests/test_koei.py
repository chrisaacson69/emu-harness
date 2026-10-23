"""The KOEI VM package (vm/koei) as moved in row F: the old-name aliases and per-title data selection.

The fold's oracle is the before/after run of every title's decompile (MIGRATION.md row F); these pin
what that run cannot show on its own."""
import subprocess
import sys

from emu_harness.core.alias import alias_package


def test_alias_gives_the_same_module_objects():
    alias_package("koei_alias_test", "emu_harness.vm.koei")
    import emu_harness.vm.koei.vm_decompile as real
    import koei_alias_test.vm_decompile as via_alias
    from koei_alias_test import vm_cfg
    assert via_alias is real
    assert vm_cfg.__name__ == "emu_harness.vm.koei.vm_cfg"


def test_python_dash_m_through_an_alias(tmp_path):
    for pkg in ("oldname", "probe_pkg"):
        (tmp_path / pkg).mkdir()
    (tmp_path / "probe_pkg" / "__init__.py").write_text("")
    (tmp_path / "probe_pkg" / "probe.py").write_text(
        "print('ran as', __name__, 'from', __file__.replace('\\\\', '/').split('/')[-2:])\n")
    (tmp_path / "oldname" / "__init__.py").write_text(
        "from emu_harness.core.alias import alias_package\nalias_package(__name__, 'probe_pkg')\n")
    r = subprocess.run([sys.executable, "-m", "oldname.probe"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "ran as __main__ from ['probe_pkg', 'probe.py']" in r.stdout      # the real file, as __main__


class _Bytes:
    def __init__(self, *b):
        self.b = bytes(b) + bytes(16)

    def read(self, a):
        return self.b[a]


def test_lengths_measured_on_the_nes_handlers():
    """F2: what every NES title's own handlers consume (tools/koei_isa_probe.py)."""
    from emu_harness.vm.koei.vm_disasm import instr_len
    assert instr_len(_Bytes(0xAD), 0) == 3                       # blkcopy: 2 operand bytes
    assert [instr_len(_Bytes(op), 0) for op in (0xE0, 0xE1, 0xE2)] == [3, 3, 3]   # bitfield #i16
    assert [instr_len(_Bytes(0xB7, e), 0) for e in (0x0B, 0x0C, 0x13, 0x14)] == [2, 4, 4, 2]
    assert instr_len(_Bytes(0xB7, 0x18), 0) == 6                 # 4-byte immediate


def test_nes_decoder_agrees_with_the_snes_table():
    import importlib.util
    from pathlib import Path
    tool = Path(__file__).resolve().parents[1] / "tools" / "koei_isa_census.py"
    snes = Path(__file__).resolve().parents[2] / "koei-snes" / "tools" / "vm_isa.py"
    if not snes.exists():
        import pytest
        pytest.skip("koei-snes is not a sibling checkout")
    spec = importlib.util.spec_from_file_location("census", tool)
    census = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(census)
    T = census.vm_isa.TABLE
    for op, s in T.primary.items():
        _, cf, size = census.nes(op)
        assert (cf, size) == (s.cf, "var" if s.size is None else s.size), f"${op:02X}"
    for e, s in T.extended.items():
        assert census.nes_ext(e) == s.size, f"$B7 ${e:02X}"


def test_the_fixed_region_is_the_last_16k_of_prg():
    """UW's 512K cart: $C000 is the last 16K of PRG (8K banks 62/63), not 16K bank 15 (F2)."""
    from emu_harness.vm.koei.cpu6502 import Memory
    prg = bytearray(0x80000)
    prg[0x7C000], prg[0x3C000] = 0xAA, 0x15
    m = Memory(b"NES\x1a" + bytes([0x20, 0, 0x52, 0]) + bytes(8) + bytes(prg))
    assert m.read(0xC000) == 0xAA


def test_na1_certified_folds_apply_to_na1_only():
    from emu_harness.vm.koei import vm_decompile as VC
    VC.set_schema("gk")
    assert VC._EXTOP_OVERRIDE == {}
    VC.set_schema("na1")
    assert 0x8327 in VC._EXTOP_OVERRIDE
