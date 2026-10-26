"""Row Q: Mesen trace-log lines -> Steps -> what the CPU did to the PPU."""
from emu_harness.backends.mesen.tracelog import parse_line
from emu_harness.machines.nes.trace import PPUWrites


def step(asm_bytes: str, a=0, x=0, y=0, extra="", pc="C000"):
    """A trace line in Mesen's short layout (no P/Fr/Cycle)."""
    return parse_line(f"{pc}  XXX {extra}   A:{a:02X} X:{x:02X} Y:{y:02X} S:FD BC:{asm_bytes}\n")


def test_parse_line_layouts():
    full = parse_line("E869  LDA (ptr3_lo),Y [$A2F2] = $3C    A:A2 X:E9 Y:00 S:F9 P:nv--dIZc "
                      "Fr:17190 Cycle:511900188 BC:B1 06")
    assert (full.pc, full.a, full.frame, full.code, full.addr, full.old) == (0xE869, 0xA2, 17190, b"\xb1\x06", 0xA2F2, 0x3C)
    short = parse_line("E97A  INY                              A:70 X:2B Y:00 S:DE BC:C8")
    assert (short.frame, short.p, short.code) == (None, "", b"\xc8")
    assert parse_line("  $87F5  LOADL_quick #13   -> L=$0064") is None


def test_ppu_writes_follow_the_registers():
    w = PPUWrites()
    w.feed_all([
        step("AD 02 20"),                       # LDA $2002: clears the toggle
        step("A9 3F"),                          # (immediate: ignored)
        step("8D 06 20", a=0x23),               # STA $2006 hi
        step("8E 06 20", x=0xC0),               # STX $2006 lo -> v = $23C0
        step("8D 07 20", a=0x55),               # STA $2007
        step("8C 17 20", y=0x66),               # STY $2017 (mirror of $2007)
        step("A9 04"), step("8D 00 20", a=0x04),   # increment 32
        step("8D 06 20", a=0x20), step("8D 06 20", a=0x00),
        step("8D 07 20", a=1), step("8D 07 20", a=2),
        step("AD 07 20"),                       # a $2007 read also advances v
        step("8D 07 20", a=3),
        step("8D 14 40", a=0x02),               # OAM DMA from $0200
        step("91 00", a=9, extra="[$2007]"),    # STA ($00),Y resolved by the emulator
    ])
    assert w.vram[0x23C0:0x23C2] == b"\x55\x66"
    assert (w.vram[0x2000], w.vram[0x2020], w.vram[0x2060]) == (1, 2, 3)
    assert [(r.start, r.increment, bytes(r.data)) for r in w.runs] == [
        (0x23C0, 1, b"\x55\x66"), (0x2000, 32, b"\x01\x02"), (0x2060, 32, b"\x03\x09")]
    assert w.oam_dma == [(None, 2)]
    assert w.vram[0x2080] == 9
