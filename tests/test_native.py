"""passes.native and cpu.m6502.coverage on a tiny program with one of each idiom (row R)."""
from emu_harness.cpu.m6502.coverage import trace_coverage
from emu_harness.cpu.m6502.cpu import CPU6502
from emu_harness.cpu.m6502.descent import Window
from emu_harness.passes.native import (NativeMap, dispatch_tables, explain, field_census, partition_edges,
                                       pointer_runs, routine_listing, stored_pointer_installs)
from emu_harness.machines.nes.memory import register_name


def program() -> bytes:
    buf = bytearray([0xFF] * 0x4000)        # $FF is not a documented opcode: data
    def put(addr, *b):
        buf[addr - 0xC000:addr - 0xC000 + len(b)] = bytes(b)
    put(0xC000, 0xBD, 0x20, 0xC1,  0x85, 0x10,  0xBD, 0x21, 0xC1,  0x85, 0x11,  0x6C, 0x10, 0x00)
    put(0xC120, 0x30, 0xC0, 0x40, 0xC0)                          # the dispatch table
    put(0xC030, 0xA9, 0x50, 0x85, 0x20, 0xA9, 0xC0, 0x85, 0x21,  # install $C050 into $20/$21
        0x20, 0x60, 0xC0, 0x60)                                  # JSR $C060 ; RTS
    put(0xC040, 0xA0, 0x03, 0xB1, 0x1E, 0x9D, 0x05, 0x04, 0x8D, 0x06, 0x20, 0x60)
    put(0xC050, 0x60)
    put(0xC060, 0x6C, 0x20, 0x00)                                # JMP ($0020): the install's consumer
    put(0xC200, 0x30, 0xC0, 0x40, 0xC0, 0x60, 0xC0)             # a pointer table in data
    return bytes(buf)


def test_the_passes_find_each_idiom():
    m = NativeMap(Window(program(), 0xC000))
    seeds = {0xC000}
    tables, sites, routines, _ = dispatch_tables(m, seeds)
    assert tables == {0xC00A: (0xC120, [0xC030, 0xC040])}
    assert {0xC030, 0xC040, 0xC060} <= routines
    from emu_harness.cpu.m6502.disasm import MODE_LEN, OPS
    _, st = m.reach(seeds)
    starts = {m.off(a): (m.byte(a), 1 + MODE_LEN[OPS[m.byte(a)][1]]) for a in st}
    assert stored_pointer_installs(m, starts) == [(0xC030, 0xC050)]
    assert pointer_runs(m) == [(0xC200, [0xC030, 0xC040, 0xC060])]
    entries = [0xC000, 0xC030, 0xC040, 0xC050, 0xC060]
    assert partition_edges(m, entries)[0xC030] == {0xC060}
    f = field_census(m, [0xC040], pointers={0x1E}, table=(0x400, 0x48F))
    assert f[3][0xC040]["kinds"] == {"r"} and f[5][0xC040]["kinds"] == {"w"}
    lines, sig = routine_listing(m, entries, 0xC040, hwname=register_name)
    assert sig["hw"] == ["PPU:ADDR"] and lines[0] == (0xC040, "ldy #$03")


class Flat:
    def __init__(self, b): self.b = b
    def read(self, a): return self.b[a & 0xFFFF]
    def write(self, a, v): self.b[a & 0xFFFF] = v & 0xFF
    def read_word(self, a): return self.read(a) | self.read(a + 1) << 8
    def write_word(self, a, v): self.write(a, v); self.write(a + 1, v >> 8)


def test_trace_coverage_records_code_edges_and_jump_targets():
    mem = bytearray(0x10000)
    mem[0xC000:0xC006] = bytes([0x20, 0x10, 0xC0, 0x4C, 0x00, 0xC0])   # JSR $C010 ; JMP $C000
    mem[0xC010:0xC013] = bytes([0x6C, 0x00, 0x02])                     # JMP ($0200)
    mem[0xC020] = 0x60                                                  # RTS
    mem[0x0200:0x0202] = bytes([0x20, 0xC0])
    cpu = CPU6502(Flat(mem))
    cpu.pc, cpu.sp = 0xC000, 0xFD
    cov = trace_coverage(cpu, Window(bytes(mem[0xC000:0x10000]), 0xC000), ticks=1, tick_every=40,
                         max_steps=1000, tick=lambda: None)
    assert (cov.steps, cov.ticks) == (40, 1)
    assert sum(cov.code) == 10 and cov.code[0x20] == 1
    assert cov.edges == {0x0: {0xC010}, 0x3: {0xC000}}
    assert cov.indirect == {0x10: {0xC020}}


def test_tables_stop_at_their_own_handler_inline_tables_and_unconsumed_pairs():
    buf = bytearray([0xFF] * 0x4000)
    def put(addr, *b):
        buf[addr - 0xC000:addr - 0xC000 + len(b)] = bytes(b)
    put(0xC000, 0xBD, 0x20, 0xC1, 0x85, 0x10, 0xBD, 0x21, 0xC1, 0x85, 0x11, 0x6C, 0x10, 0x00)
    put(0xC120, 0x30, 0xC0, 0x24, 0xC1)                          # two handlers, the 2nd right after
    put(0xC124, 0xA9, 0xC0, 0x60)                                # lda #$C0: reads as the word $C0A9
    put(0xC0A9, 0x60)                                            # ...and $C0A9 decodes, so it looks like a 3rd
    put(0xC030, 0xA9, 0x50, 0x85, 0x20, 0xA9, 0xC0, 0x85, 0x21,  # $C050 into $20/$21 (consumed)
        0xA9, 0x60, 0x85, 0x30, 0xA9, 0xC0, 0x85, 0x31, 0x60)    # $C060 into $30/$31 (never jumped through)
    put(0xC050, 0x6C, 0x20, 0x00)
    put(0xC060, 0x60)
    put(0xC040, 0x20, 0x80, 0xC0, 0x60, 0xC0, 0x24, 0xC1)        # jsr dispatcher; .word $C060, $C124
    put(0xC080, 0x68, 0x68, 0x60)                                # pla pla (the real one jumps on)
    m = NativeMap(Window(bytes(buf), 0xC000))
    seeds = {0xC000, 0xC040, 0xC050}                             # $C050: the JMP (ind) consumer
    tables, sites, routines, starts = dispatch_tables(m, seeds, inline=[0xC080])
    assert tables == {0xC00A: (0xC120, [0xC030, 0xC124]), 0xC040: (0xC043, [0xC060, 0xC124])}
    assert 0xC0A9 not in routines and 0xC043 not in m.descent.starts
    assert m.conflicts() == {}
    from emu_harness.cpu.m6502.disasm import MODE_LEN, OPS
    st = {n: (op, 1 + MODE_LEN[OPS[op][1]]) for n, op in starts.items()}
    assert stored_pointer_installs(m, st) == [(0xC030, 0xC050)]


def test_explain_names_bit_skip_and_dead_paths_only():
    buf = bytearray([0xFF] * 0x4000)
    def put(addr, *b):
        buf[addr - 0xC000:addr - 0xC000 + len(b)] = bytes(b)
    put(0xC000, 0xA2, 0x01, 0x2C, 0xA2, 0x02, 0x60)    # ldx #1 / bit $02A2 (= ldx #2 at $C003) / rts
    put(0xC010, 0xD0, 0x04, 0xF0, 0x02, 0xFF, 0xFF, 0x60)  # bne / beq to $C016: $C014 is never run
    put(0xC020, 0xD0, 0x01, 0x60)                      # bne alone to $C023 ($FF): a real halt
    m = NativeMap(Window(bytes(buf), 0xC000))
    m.reach({0xC000, 0xC003, 0xC010})
    c = m.conflicts()
    assert c == {"overlap": [0xC003], "illegal": [0xC014]}
    assert explain(m, "overlap", 0xC003) == "bit-skip" and explain(m, "illegal", 0xC014) == "dead-path"
    m.reach({0xC020})
    assert m.conflicts() == {"illegal": [0xC023]} and explain(m, "illegal", 0xC023) is None
