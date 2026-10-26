"""machines.nes.bus: a cartridge that switches its own banks (row P2, 2026-10-26)."""
import pytest

from emu_harness.machines.nes.bus import Bus


def mmc5_rom(banks: int = 8) -> bytes:
    """iNES MMC5 image whose every 8K bank is filled with its own number."""
    header = b"NES\x1a" + bytes([banks // 2, 0, 0x50, 0x00]) + bytes(8)
    return header + b"".join(bytes([n]) * 0x2000 for n in range(banks))


def test_mmc5_windows_switch_by_register_and_wrap():
    b = Bus(mmc5_rom())
    assert [b.read(a) for a in (0x8000, 0xA000, 0xC000, 0xE000)] == [0, 0, 6, 7]
    b.write(0x5114, 0x83)
    b.write(0x5115, 0x80 | 0x0A)                       # bank 10 on an 8-bank cart wraps to 2
    assert (b.read(0x8123), b.read(0xA000), b.prg_bank(0xA000)) == (3, 2, 2)


def test_mmc5_ram_window_and_multiplier():
    b = Bus(mmc5_rom())
    b.write(0x5116, 0x00)                              # bit 7 clear: PRG-RAM at $C000
    b.write(0xC010, 0x5A)
    assert b.read(0x6010) == 0x5A                      # one 8K chip
    b.write(0x5205, 200); b.write(0x5206, 3)
    assert (b.read(0x5205), b.read(0x5206)) == (600 & 0xFF, 600 >> 8)
    with pytest.raises(NotImplementedError):
        b.write(0x5100, 1)


def test_ram_mirrors_and_controller_shifts_out():
    b = Bus(mmc5_rom())
    b.write(0x0805, 9)
    assert b.read(0x0005) == 9 and b.read(0x1805) == 9
    b.buttons = 0b1000_1001                            # A, Start, Right
    b.write(0x4016, 1); b.write(0x4016, 0)
    assert [b.read(0x4016) for _ in range(9)] == [1, 0, 0, 1, 0, 0, 0, 1, 1]


def nrom_rom() -> bytes:
    return b"NES\x1a" + bytes([1, 0, 0x01, 0x00]) + bytes(8) + bytes(0x4000)


def test_ppu_port_receives_register_traffic():
    """Bus(ppu=PPUPort()): a headless run's PPU writes land in VRAM (Mappy render_level)."""
    from emu_harness.machines.nes.ppuport import PPUPort
    port = PPUPort()
    bus = Bus(nrom_rom(), ppu=port)
    bus.write(0x2006, 0x21)                   # toggle now set: a stray first half
    bus.read(0x2002)                          # $2002 read clears it
    for v in (0x20, 0x00):                    # $2006 hi, lo -> v = $2000
        bus.write(0x2006, v)
    bus.write(0x2000, 0x04)                   # increment 32
    bus.write(0x2007, 0xAA)
    bus.write(0x200F, 0xBB)                   # mirror of $2007
    assert (port.vram[0x2000], port.vram[0x2020]) == (0xAA, 0xBB)
    bus.write(0x2000, 0x00)
    bus.write(0x2006, 0x20); bus.write(0x2006, 0x00)
    assert bus.read(0x2007) == 0              # first read returns the stale buffer
    assert bus.read(0x2007) == 0xAA           # then the byte at $2000
    assert port.v == 0x2002


def test_bus_without_a_port_stays_a_stub():
    bus = Bus(nrom_rom())
    bus.write(0x2006, 0x20)
    assert bus.read(0x2007) == 0
    assert bus.read(0x2002) in (0x00, 0x80)
