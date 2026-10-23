"""The KOEI VM package (vm/koei) as moved in row F: the old-name aliases and per-title data selection.

The fold's oracle is the before/after run of every title's decompile (MIGRATION.md row F); these pin
what that run cannot show on its own."""
import subprocess
import sys

from emu_harness.core.alias import alias_package


def test_alias_gives_the_same_module_objects():
    alias_package("koei_alias_test", "emu_harness.vm.koei", also=("emu_harness.vm.koei.nes",))
    import emu_harness.vm.koei.nes.vm_decompile as real
    import koei_alias_test.vm_decompile as via_alias
    from koei_alias_test import vm_cfg
    assert via_alias is real
    assert vm_cfg.__name__ == "emu_harness.vm.koei.nes.vm_cfg"


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
    from emu_harness.vm.koei.nes.vm_disasm import instr_len
    assert instr_len(_Bytes(0xAD), 0) == 3                       # blkcopy: 2 operand bytes
    assert [instr_len(_Bytes(op), 0) for op in (0xE0, 0xE1, 0xE2)] == [3, 3, 3]   # bitfield #i16
    assert [instr_len(_Bytes(0xB7, e), 0) for e in (0x0B, 0x0C, 0x13, 0x14)] == [2, 4, 4, 2]
    assert instr_len(_Bytes(0xB7, 0x18), 0) == 6                 # 4-byte immediate


def test_nes_decoder_agrees_with_the_snes_table():
    import importlib.util
    from pathlib import Path
    tool = Path(__file__).resolve().parents[1] / "tools" / "koei_isa_census.py"
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
    from emu_harness.vm.koei.nes.cpu6502 import Memory
    prg = bytearray(0x80000)
    prg[0x7C000], prg[0x3C000] = 0xAA, 0x15
    m = Memory(b"NES\x1a" + bytes([0x20, 0, 0x52, 0]) + bytes(8) + bytes(prg))
    assert m.read(0xC000) == 0xAA


def test_na1_certified_folds_apply_to_na1_only():
    from emu_harness.vm.koei.nes import vm_decompile as VC
    VC.set_schema("gk")
    assert VC._EXTOP_OVERRIDE == {}
    VC.set_schema("na1")
    assert 0x8327 in VC._EXTOP_OVERRIDE



def test_dream_switch_merging_into_its_loop_header():
    """rot3k2-SNES root $34F4: a switch whose merge is its enclosing loop's header (the merge
    dominates the switch). Excluding the merge's dominance emptied the switch region, orphaned the
    case bodies, and a sink became a loop 'header' -- DREAM crashed emitting it (row G). Needs the
    sibling rot3k2-snes-decompiler checkout and its (gitignored) ROM."""
    import argparse
    from pathlib import Path
    import pytest
    T = Path(__file__).resolve().parents[2] / "rot3k2-snes-decompiler"
    sfc = T / "Romance of the Three Kingdoms II (USA).sfc"
    if not sfc.exists():
        pytest.skip("rot3k2-snes-decompiler ROM not present")
    from emu_harness.machines.snes.mapper import strip_copier_header
    from emu_harness.vm.koei.snes import profile, vm_isa, vm_lift, vm_walk, vm_dream
    from emu_harness.vm.koei.snes.symbols import Symbols
    rom, _ = strip_copier_header(sfc.read_bytes())
    P = profile.select("rot3k2", rom)
    space = vm_walk.Space(rom, P, vm_walk.module_from_args(
        rom, P, argparse.Namespace(module="root", start=None, base=None)))
    isa = vm_isa.bind(rom, P)
    W = vm_walk.walk_module(isa, space)
    R = vm_lift.Lifter(isa, space, Symbols(str(T / "data" / "root"), profile=P), W).lift(0x34F4, W.walks[0x34F4])
    _lines, how = vm_dream.structure(R)
    # Built, and equivalent by the gate; only the duplication cap keeps it out of the C. Before
    # the fix: a TypeError in emission (or, with just the sink guard, 'out of scope').
    assert how in ("dream", "goto: DREAM over-dup")


def test_match_tokens_come_from_bytes_not_names():
    """vm/koei/match: ROM operands and control targets relocate ('A'); RAM operands, immediates and
    frame offsets stay; switch tables keep their case values; ext ops carry their ext byte."""
    from emu_harness.vm.koei.match import token
    assert token([0x0C]) == "0C"                                   # nibble op: operand in the opcode
    assert token([0xD6, 0x17, 0x80]) == token([0xD6, 0x34, 0x92]) == "D6 A"   # a jump, anywhere
    assert token([0xA4, 0x01, 0x71]) == "A4 $7101"                 # a RAM global stays
    assert token([0xA4, 0x00, 0x91]) == "A4 A"                     # a ROM table relocates
    sw = [0xD9, 2, 0, 5, 0, 0x10, 0x80, 9, 0, 0x20, 0x80, 0x30, 0x80]   # n=2: (5 ->), (9 ->), default
    assert token(sw) == "D9 2 5 A 9 A A"
    assert token([0xB7, 0x18, 1, 2, 3, 4]).startswith("B7.18 ")
