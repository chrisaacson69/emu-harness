"""The shared 6502 core (cpu/m6502/cpu.py).

The main oracle is nestest's golden log (tools/nestest.py); it runs here when the ROM and log are
in _scratch/nestest/ (third-party test data, never committed). The rest pins what nestest does not
reach: the exact opcode set and the interrupt sequence."""
from pathlib import Path

import pytest

from emu_harness.cpu.m6502.cpu import CPU6502
from emu_harness.cpu.m6502.disasm import OPS

NESTEST = Path(__file__).resolve().parents[1] / "_scratch" / "nestest"


class Flat:
    """64K of RAM."""
    def __init__(self):
        self.b = bytearray(0x10000)

    def read(self, a):
        return self.b[a & 0xFFFF]

    def write(self, a, v):
        self.b[a & 0xFFFF] = v & 0xFF

    def read_word(self, a):
        return self.read(a) | self.read(a + 1) << 8

    def write_word(self, a, v):
        self.write(a, v)
        self.write(a + 1, v >> 8)


def cpu_at(pc, *code):
    m = Flat()
    m.b[pc:pc + len(code)] = bytes(code)
    c = CPU6502(m)
    c.pc = pc
    return c, m


@pytest.mark.skipif(not (NESTEST / "nestest.log").exists(), reason="nestest not fetched into _scratch/")
def test_nestest_documented_section():
    import importlib.util
    spec = importlib.util.spec_from_file_location("nestest", Path(__file__).resolve().parents[1] / "tools/nestest.py")
    nt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nt)
    n, err = nt.run(CPU6502)
    assert err is None and n == 5003


def test_exactly_the_documented_opcodes():
    for op in range(256):
        c, _ = cpu_at(0x200, op, 0, 0)
        try:
            c.step()
            implemented = True
        except NotImplementedError:
            implemented = False
        assert implemented == (op in OPS), f"${op:02X}"


def test_brk_pushes_pc_plus_2_and_real_flags_with_b():
    c, m = cpu_at(0x0300, 0x00, 0xEA)
    m.write_word(0xFFFE, 0x1234)
    c.p = 0xC3                                    # N V Z C set, I clear
    c.step()
    assert c.pc == 0x1234 and c.i
    assert m.read(0x01FD) == 0x03 and m.read(0x01FC) == 0x02   # return address $0302 (pc+2)
    assert m.read(0x01FB) == 0xC3 | 0x30                        # real flags + B + bit 5


def test_nmi_pushes_flags_without_b_and_rti_restores():
    c, m = cpu_at(0x0400, 0x40)                  # the handler is a bare RTI at $0400
    m.write_word(0xFFFA, 0x0400)
    c.pc, c.p = 0x0555, 0x81                      # N, C set
    c.nmi()
    assert c.pc == 0x0400 and c.i and m.read(0x01FB) == 0xA1   # B clear, bit 5 set
    c.step()                                      # RTI
    assert c.pc == 0x0555 and c.p == 0xA1 & ~0x10 | 0x20 and not c.i


def test_irq_is_masked_by_i():
    c, m = cpu_at(0x0500)
    m.write_word(0xFFFE, 0x0600)
    c.i = True
    assert not c.irq() and c.pc == 0x0500
    c.i = False
    assert c.irq() and c.pc == 0x0600


def test_php_sets_b_plp_ignores_it():
    c, m = cpu_at(0x0200, 0x08, 0x28)
    c.p = 0x01
    c.step()
    assert m.read(0x01FD) == 0x31
    c.step()
    assert c.p == 0x21
