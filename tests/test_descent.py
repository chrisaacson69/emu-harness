"""The shared 6502 recursive descent (cpu/m6502/descent.py), on hand-assembled windows.

The fold's real oracle is each title fork's output on its home ROM (MIGRATION.md row B); these
pin the walker's rules so a change to one shows up without a ROM."""
from emu_harness.cpu.m6502.descent import Window, descend, flow, gaps, verdicts
from emu_harness.cpu.m6502.disasm import listing_verdicts


def win(*code, base=0x8000, size=0x40, mirrors=()):
    buf = bytearray([0xFF] * size)                 # $FF is undocumented: a walk stops on it
    buf[:len(code)] = bytes(code)
    return Window(bytes(buf), base, mirrors)


def test_branch_both_sides_and_jsr_falls_through():
    w = win(0xD0, 0x03,          # 8000 bne 8005
            0x20, 0x10, 0x80,    # 8002 jsr 8010
            0x60,                # 8005 rts
            *[0xFF] * 10,
            0xA9, 0x01, 0x60)    # 8010 lda #1 / rts
    d = descend(w, [0x8000])
    assert d.entries == {0x8000, 0x8010}
    assert d.flows[0x8000].starts == {0x8000, 0x8002, 0x8005}
    assert d.flows[0x8000].calls == {0x8002: 0x8010}
    assert d.starts == {0x8000, 0x8002, 0x8005, 0x8010, 0x8012}
    assert d.operands == {0x8001, 0x8003, 0x8004, 0x8011}
    assert d.callers() == {0x8010: {0x8000}}


def test_jmp_followed_in_window_not_an_entry():
    w = win(0x4C, 0x05, 0x80,    # 8000 jmp 8005
            0xFF, 0xFF,
            0x4C, 0x00, 0x90)    # 8005 jmp $9000 (outside the window)
    f = flow(w, 0x8000)
    assert f.starts == {0x8000, 0x8005}
    assert f.jumps == {0x8000: 0x8005, 0x8005: 0x9000}
    assert descend(w, [0x8000]).entries == {0x8000}
    assert flow(w, 0x8000, into_jmp=False).starts == {0x8000}


def test_ends_rti_jmp_ind_illegal_and_optional_brk():
    for end in ([0x40], [0x6C, 0x00, 0x02], [0x02]):
        assert flow(win(*end, 0xEA), 0x8000).starts <= {0x8000}
    assert flow(win(0x00, 0xEA, 0x60), 0x8000).starts == {0x8000, 0x8001, 0x8002}   # brk: 1 byte
    assert flow(win(0x00, 0xEA, 0x60), 0x8000, stop_brk=True).starts == {0x8000}


def test_inline_bridge_skips_its_operand_bytes():
    w = win(0x20, 0x99, 0xE2, 0x00, 0xFE, 0xFF,   # jsr $E299 + 3 inline bytes
            0x60)                                 # 8006 rts
    plain = flow(w, 0x8000)
    assert 0x8003 in plain.starts                 # without the bridge the inline bytes decode
    f = flow(w, 0x8000, inline={0xE299: 3})
    assert f.starts == {0x8000, 0x8006}
    assert f.inline == {0x8003, 0x8004, 0x8005}


def test_follow_vetoes_jsr_and_jmp_targets_not_branches():
    w = win(0x20, 0x08, 0x80,    # 8000 jsr 8008 (a "stub")
            0x4C, 0x08, 0x80,    # 8003 jmp 8008
            0xFF, 0xFF,
            0x60)                # 8008 rts
    d = descend(w, [0x8000], follow=lambda a: a != 0x8008)
    assert d.entries == {0x8000}
    assert 0x8008 not in d.starts


def test_mirrored_window_canonicalizes():
    w = win(0x20, 0x05, 0x80,    # C000 jsr $8005: the same byte as $C005
            0x60, 0xFF,
            0x60, base=0xC000, mirrors=(0x8000,))
    d = descend(w, [0x8000])     # seed through the mirror
    assert d.entries == {0xC000, 0xC005}
    assert d.starts == {0xC000, 0xC003, 0xC005}


def test_data_end_and_truncation():
    w = win(0xEA, 0xEA, 0x60)
    assert flow(w, 0x8000, data=frozenset({0x8001})).starts == {0x8000}
    assert flow(w, 0x8000, end=0x8002).starts == {0x8000, 0x8001}
    t = Window(bytes([0xEA, 0xAD, 0x00]), 0x8000)  # lda abs needs 3 bytes, 2 remain
    assert flow(t, 0x8000).starts == {0x8000}


def test_overlaps_and_verdicts():
    w = win(0xD0, 0x01,          # 8000 bne 8003: lands inside the lda's operand
            0xA9, 0xEA,          # 8002 lda #$EA   (8003 = $EA, a nop when entered)
            0x60)                # 8004 rts
    d = descend(w, [0x8000])
    assert d.overlaps() == {0x8003}
    v = verdicts(w, d.starts, d.operands)
    assert v[:5] == ["I", "O", "I", "I", "I"] and v[5] == ""


def test_gaps_need_a_decodable_prologue():
    w = win(*[0xEA] * 8, *[0x60] * 4, 0xA9, 0x01, *[0xEA] * 6, 0x60, *[0x02] * 8)
    covered = set(range(0x8008, 0x800C)) | {0x8014}
    # uncovered runs: $8000 (8 nops), $800C (lda # + 6 nops = 7 ops), $8015 ($02 x8, then $FF fill)
    assert gaps(w, covered) == [0x8000, 0x800C]
    assert gaps(w, covered, min_ops=8) == [0x8000]
    assert gaps(w, covered | {0x8000}) == [0x800C]                  # $8001 run: 7 bytes, too short
    assert gaps(w, covered, pad=0xEA) == [0x800C]                   # the $8000 run starts with it


def test_listing_verdicts():
    text = ("b00_8000:   lda #$01                    ; 0:  a9 01\n"
            "            hex 12 34               ; 2:  12 34\n")
    assert listing_verdicts(text, 5) == ["I", "O", "D", "D", ""]


def test_halts_name_what_stopped_a_path():
    w = win(0xD0, 0x02,          # 8000 bne 8004: the fall-through runs into $02
            0x02, 0xFF,          # 8002 undocumented
            0xEA, 0x00)          # 8004 nop, 8005 brk
    f = flow(w, 0x8000, stop_brk=True)
    assert f.halts == {0x8002: "illegal", 0x8005: "brk"}
    assert flow(w, 0x8004, data=frozenset({0x8005})).halts == {0x8005: "data"}
    assert flow(w, 0x8004).halts == {0x8006: "illegal"}        # brk is an instruction without stop_brk; $FF pad
    assert flow(Window(bytes([0xEA]), 0x8000), 0x8000).halts == {0x8001: "window"}
    d = descend(w, [0x8000, 0x8004], stop_brk=True)
    assert d.halts() == {0x8002: "illegal", 0x8005: "brk"}


def test_joins_and_terminators_are_not_halts():
    w = win(0xD0, 0x01, 0xEA, 0x60)   # 8000 bne 8003 / 8002 nop / 8003 rts: both paths join at the rts
    assert flow(w, 0x8000).halts == {}
