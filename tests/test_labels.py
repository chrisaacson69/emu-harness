"""Row A (label export): the registry reader, the NES locator, the Mesen writer.

Expected values are the ones the old per-repo exporters produced on real registries
(MIGRATION.md, row A oracle), restated here so the rules survive the old tools' retirement.
"""
import pytest

from emu_harness.backends.mesen.labels import format_label, render_mlb
from emu_harness.core.symbols import Symbol, read_registry
from emu_harness.machines.nes.memory import Cart, locate


def ines(tmp_path, mapper, prg_16k, battery):
    h = bytes([0x4E, 0x45, 0x53, 0x1A, prg_16k, 0, ((mapper & 0xF) << 4) | (2 if battery else 0), mapper & 0xF0])
    p = tmp_path / "game.nes"
    p.write_bytes(h + bytes(8) + bytes(prg_16k * 0x4000))
    return Cart.from_ines(p)


# --- the cartridge comes from the header -------------------------------------------------

def test_cart_from_header(tmp_path):
    assert ines(tmp_path, 5, 16, True) == Cart(5, 256 * 1024, True)
    assert ines(tmp_path, 0, 1, False) == Cart(0, 16 * 1024, False)


def test_unsupported_mapper_is_loud(tmp_path):
    with pytest.raises(NotImplementedError):
        ines(tmp_path, 4, 8, False)


# --- locate ------------------------------------------------------------------------------

MMC1 = Cart(1, 256 * 1024, True)     # NA1
MMC5 = Cart(5, 256 * 1024, True)     # the KOEI MMC5 titles
NROM = Cart(0, 16 * 1024, False)     # Mappy


@pytest.mark.parametrize("cart,section,addr,want", [
    (MMC1, "prg.bank3", 0x8D5C, ("nes.prg_rom", 3 * 0x4000 + 0x0D5C)),
    (MMC1, "prg.bank0", 0xC7C9, ("nes.prg_rom", 0x3C7C9)),        # fixed bank: address wins over section
    (MMC5, "prg.bank13", 0xA3F4, ("nes.prg_rom", 13 * 0x2000 + 0x03F4)),
    (MMC5, "prg.bank9", 0xC123, ("nes.prg_rom", 30 * 0x2000 + 0x0123)),   # $C000-$DFFF = second-to-last
    (MMC5, "prg.bank31", 0xE509, ("nes.prg_rom", 31 * 0x2000 + 0x0509)),
    (NROM, "prg.bank0", 0xC14F, ("nes.prg_rom", 0x014F)),          # 16K mirrored
    (MMC5, "prg.bank17", 0x0B0A, None),                            # not a PRG CPU address
    (MMC1, "ram", 0x0073, ("nes.internal_ram", 0x73)),
    (MMC1, "ram", 0x0873, ("nes.internal_ram", 0x73)),              # $0800-$1FFF mirrors
    (MMC1, "ram", 0x6D63, ("nes.save_ram", 0x0D63)),
    (NROM, "ram", 0x6000, ("nes.work_ram", 0)),
    (MMC5, "ram", 0x5105, ("nes.cpu_bus", 0x5105)),
    (MMC5, "ram", 0x9D2C, ("nes.cpu_bus", 0x9D2C)),
])
def test_locate(cart, section, addr, want):
    assert locate(cart, section, addr) == want


# --- the writer: Mesen's CodeLabel.ToString ----------------------------------------------

def test_format_matches_mesen_export():
    assert format_label(Symbol("nes.prg_rom", 0x14F, "rts_noop")) == "NesPrgRom:014F:rts_noop"
    assert format_label(Symbol("nes.save_ram", 0x19A2, "m", "a: b")) == "NesSaveRam:19A2:m:a: b"
    assert format_label(Symbol("nes.prg_rom", 0x3FAD6, "msg", "too\nweak")) == "NesPrgRom:3FAD6:msg:too\\nweak"
    assert format_label(Symbol("snes.work_ram", 0x2003, "entry", "  ")) == "SnesWorkRam:2003:entry"


def test_writer_drops_what_mesen_would_drop_and_says_so():
    text, problems = render_mlb([
        Symbol("nes.prg_rom", 0x10, "first"),
        Symbol("nes.prg_rom", 0x10, "second"),
        Symbol("snes.work_ram", 0x7046, "2d_s"),
    ])
    assert text == "NesPrgRom:0010:first\n"
    assert len(problems) == 2


# --- the reader: every departure from TOML is a reported defect ---------------------------

def test_registry_defects(tmp_path):
    p = tmp_path / "mesen-labels.toml"
    p.write_text(
        '[prg.bank1]\n'
        '"0x8000" = { name = "a", comment = "has a: colon" }\n'
        '"0x8000" = { name = "b" }\n'
        '"0x8001" = { name = "c", comment = "[STR] "Hi" there" }\n'
        '[prg.bank1]  # again\n'
        '"0x8001" = { name = "c" }\n', encoding="utf-8")
    reg = read_registry(p)
    assert [(e.name, e.comment) for e in reg.entries] == [("a", "has a: colon"), ("c", '[STR] "Hi" there')]
    assert sorted(d.kind for d in reg.defects) == ["conflict", "duplicate", "invalid-toml", "repeated-section"]
