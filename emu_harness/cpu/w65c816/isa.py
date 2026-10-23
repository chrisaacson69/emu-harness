#!/usr/bin/env python3
"""
w65c816 -- a pure WDC 65C816 instruction decoder (CPU level, system-agnostic).

This is the 65C816 analog of the NES project's `disasm6502`: it knows the CPU and
nothing else. It decodes ONE instruction from a flat byte buffer, given the current
CPU (bank, addr) for display/target math and the current M/X flag widths, and
returns the decoded instruction plus the (possibly updated) widths. It has NO
knowledge of SNES memory mapping, ROM headers, or files -- a caller wires those in
(see `disasm65816.py`, the SNES driver). Because it's dependency-free it can
graduate to its own repo the moment a second 65C816 system needs it.

The defining 65C816 subtlety this handles: immediate operand LENGTH is runtime
state. `LDA #` reads 2 bytes when the M (accumulator) flag is 16-bit, 1 when 8-bit;
`LDX/LDY/CPX/CPY #` follow the X (index) flag. REP/SEP change those flags. Decode
without tracking them and the instruction stream misaligns.

Opcode table = the WDC 65C816 matrix, cross-checked against 6502.org's
65c816opcodes reference (all mnemonics/modes/lengths verified).
"""

# Addressing mode -> operand byte count (None = dynamic, resolved from M/X flag).
# Instruction length = 1 (opcode) + operand bytes.
OPERAND_BYTES = {
    "imp": 0, "acc": 0,
    "imm_m": None, "imm_x": None,   # width from M / X flag
    "imm8": 1,
    "dp": 1, "dpx": 1, "dpy": 1,
    "idp": 1, "idx": 1, "idy": 1, "idl": 1, "idly": 1,
    "sr": 1, "sry": 1, "pei": 1,
    "abs": 2, "abx": 2, "aby": 2, "ind": 2, "iax": 2, "ial": 2, "pea": 2,
    "abl": 3, "alx": 3,
    "rel": 1, "rell": 2,
    "bm": 2,
}

# 256 opcodes indexed by opcode byte. (mnemonic, addressing mode).
OPCODES = [
    # 0x00
    ("BRK","imm8"),("ORA","idx"),("COP","imm8"),("ORA","sr"),
    ("TSB","dp"),("ORA","dp"),("ASL","dp"),("ORA","idl"),
    ("PHP","imp"),("ORA","imm_m"),("ASL","acc"),("PHD","imp"),
    ("TSB","abs"),("ORA","abs"),("ASL","abs"),("ORA","abl"),
    # 0x10
    ("BPL","rel"),("ORA","idy"),("ORA","idp"),("ORA","sry"),
    ("TRB","dp"),("ORA","dpx"),("ASL","dpx"),("ORA","idly"),
    ("CLC","imp"),("ORA","aby"),("INC","acc"),("TCS","imp"),
    ("TRB","abs"),("ORA","abx"),("ASL","abx"),("ORA","alx"),
    # 0x20
    ("JSR","abs"),("AND","idx"),("JSL","abl"),("AND","sr"),
    ("BIT","dp"),("AND","dp"),("ROL","dp"),("AND","idl"),
    ("PLP","imp"),("AND","imm_m"),("ROL","acc"),("PLD","imp"),
    ("BIT","abs"),("AND","abs"),("ROL","abs"),("AND","abl"),
    # 0x30
    ("BMI","rel"),("AND","idy"),("AND","idp"),("AND","sry"),
    ("BIT","dpx"),("AND","dpx"),("ROL","dpx"),("AND","idly"),
    ("SEC","imp"),("AND","aby"),("DEC","acc"),("TSC","imp"),
    ("BIT","abx"),("AND","abx"),("ROL","abx"),("AND","alx"),
    # 0x40
    ("RTI","imp"),("EOR","idx"),("WDM","imm8"),("EOR","sr"),
    ("MVP","bm"),("EOR","dp"),("LSR","dp"),("EOR","idl"),
    ("PHA","imp"),("EOR","imm_m"),("LSR","acc"),("PHK","imp"),
    ("JMP","abs"),("EOR","abs"),("LSR","abs"),("EOR","abl"),
    # 0x50
    ("BVC","rel"),("EOR","idy"),("EOR","idp"),("EOR","sry"),
    ("MVN","bm"),("EOR","dpx"),("LSR","dpx"),("EOR","idly"),
    ("CLI","imp"),("EOR","aby"),("PHY","imp"),("TCD","imp"),
    ("JML","abl"),("EOR","abx"),("LSR","abx"),("EOR","alx"),
    # 0x60
    ("RTS","imp"),("ADC","idx"),("PER","rell"),("ADC","sr"),
    ("STZ","dp"),("ADC","dp"),("ROR","dp"),("ADC","idl"),
    ("PLA","imp"),("ADC","imm_m"),("ROR","acc"),("RTL","imp"),
    ("JMP","ind"),("ADC","abs"),("ROR","abs"),("ADC","abl"),
    # 0x70
    ("BVS","rel"),("ADC","idy"),("ADC","idp"),("ADC","sry"),
    ("STZ","dpx"),("ADC","dpx"),("ROR","dpx"),("ADC","idly"),
    ("SEI","imp"),("ADC","aby"),("PLY","imp"),("TDC","imp"),
    ("JMP","iax"),("ADC","abx"),("ROR","abx"),("ADC","alx"),
    # 0x80
    ("BRA","rel"),("STA","idx"),("BRL","rell"),("STA","sr"),
    ("STY","dp"),("STA","dp"),("STX","dp"),("STA","idl"),
    ("DEY","imp"),("BIT","imm_m"),("TXA","imp"),("PHB","imp"),
    ("STY","abs"),("STA","abs"),("STX","abs"),("STA","abl"),
    # 0x90
    ("BCC","rel"),("STA","idy"),("STA","idp"),("STA","sry"),
    ("STY","dpx"),("STA","dpx"),("STX","dpy"),("STA","idly"),
    ("TYA","imp"),("STA","aby"),("TXS","imp"),("TXY","imp"),
    ("STZ","abs"),("STA","abx"),("STZ","abx"),("STA","alx"),
    # 0xA0
    ("LDY","imm_x"),("LDA","idx"),("LDX","imm_x"),("LDA","sr"),
    ("LDY","dp"),("LDA","dp"),("LDX","dp"),("LDA","idl"),
    ("TAY","imp"),("LDA","imm_m"),("TAX","imp"),("PLB","imp"),
    ("LDY","abs"),("LDA","abs"),("LDX","abs"),("LDA","abl"),
    # 0xB0
    ("BCS","rel"),("LDA","idy"),("LDA","idp"),("LDA","sry"),
    ("LDY","dpx"),("LDA","dpx"),("LDX","dpy"),("LDA","idly"),
    ("CLV","imp"),("LDA","aby"),("TSX","imp"),("TYX","imp"),
    ("LDY","abx"),("LDA","abx"),("LDX","aby"),("LDA","alx"),
    # 0xC0
    ("CPY","imm_x"),("CMP","idx"),("REP","imm8"),("CMP","sr"),
    ("CPY","dp"),("CMP","dp"),("DEC","dp"),("CMP","idl"),
    ("INY","imp"),("CMP","imm_m"),("DEX","imp"),("WAI","imp"),
    ("CPY","abs"),("CMP","abs"),("DEC","abs"),("CMP","abl"),
    # 0xD0
    ("BNE","rel"),("CMP","idy"),("CMP","idp"),("CMP","sry"),
    ("PEI","pei"),("CMP","dpx"),("DEC","dpx"),("CMP","idly"),
    ("CLD","imp"),("CMP","aby"),("PHX","imp"),("STP","imp"),
    ("JML","ial"),("CMP","abx"),("DEC","abx"),("CMP","alx"),
    # 0xE0
    ("CPX","imm_x"),("SBC","idx"),("SEP","imm8"),("SBC","sr"),
    ("CPX","dp"),("SBC","dp"),("INC","dp"),("SBC","idl"),
    ("INX","imp"),("SBC","imm_m"),("NOP","imp"),("XBA","imp"),
    ("CPX","abs"),("SBC","abs"),("INC","abs"),("SBC","abl"),
    # 0xF0
    ("BEQ","rel"),("SBC","idy"),("SBC","idp"),("SBC","sry"),
    ("PEA","pea"),("SBC","dpx"),("INC","dpx"),("SBC","idly"),
    ("SED","imp"),("SBC","aby"),("PLX","imp"),("XCE","imp"),
    ("JSR","iax"),("SBC","abx"),("INC","abx"),("SBC","alx"),
]

# Instructions that end a straight-line run (unconditional control transfer).
TERMINATORS = {"RTS", "RTL", "RTI", "JMP", "JML", "BRA", "BRL", "STP"}
BRANCHES = {"JMP", "JML", "JSR", "JSL", "BRA", "BRL",
            "BCC", "BCS", "BEQ", "BNE", "BMI", "BPL", "BVC", "BVS"}


def _u(buf, off, n):
    v = 0
    for i in range(n):
        v |= buf[off + i] << (8 * i)
    return v


def _signed(v, bits):
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def operand_len(mode, m8, x8):
    n = OPERAND_BYTES[mode]
    if n is not None:
        return n
    if mode == "imm_m":
        return 1 if m8 else 2
    if mode == "imm_x":
        return 1 if x8 else 2
    raise KeyError(mode)


def _format_operand(mode, buf, off, olen, cur_bank, next_addr):
    """Return (text, target) where target is a (bank, addr) dest or None."""
    if mode == "imp":
        return "", None
    if mode == "acc":
        return "A", None
    if mode in ("imm_m", "imm_x", "imm8"):
        return "#$%0*X" % (olen * 2, _u(buf, off, olen)), None
    if mode == "dp":
        return "$%02X" % buf[off], None
    if mode == "dpx":
        return "$%02X,X" % buf[off], None
    if mode == "dpy":
        return "$%02X,Y" % buf[off], None
    if mode == "idp":
        return "($%02X)" % buf[off], None
    if mode == "idx":
        return "($%02X,X)" % buf[off], None
    if mode == "idy":
        return "($%02X),Y" % buf[off], None
    if mode == "idl":
        return "[$%02X]" % buf[off], None
    if mode == "idly":
        return "[$%02X],Y" % buf[off], None
    if mode == "sr":
        return "$%02X,S" % buf[off], None
    if mode == "sry":
        return "($%02X,S),Y" % buf[off], None
    if mode == "pei":
        return "($%02X)" % buf[off], None
    if mode in ("abs", "pea"):
        a = _u(buf, off, 2)
        return "$%04X" % a, ((cur_bank, a) if mode == "abs" else None)
    if mode == "abx":
        return "$%04X,X" % _u(buf, off, 2), None
    if mode == "aby":
        return "$%04X,Y" % _u(buf, off, 2), None
    if mode == "ind":
        return "($%04X)" % _u(buf, off, 2), None
    if mode == "iax":
        return "($%04X,X)" % _u(buf, off, 2), None
    if mode == "ial":
        return "[$%04X]" % _u(buf, off, 2), None
    if mode == "abl":
        a = _u(buf, off, 3)
        return "$%06X" % a, (a >> 16, a & 0xFFFF)
    if mode == "alx":
        return "$%06X,X" % _u(buf, off, 3), None
    if mode == "rel":
        t = (next_addr + _signed(buf[off], 8)) & 0xFFFF
        return "$%04X" % t, (cur_bank, t)
    if mode == "rell":
        t = (next_addr + _signed(_u(buf, off, 2), 16)) & 0xFFFF
        return "$%04X" % t, (cur_bank, t)
    if mode == "bm":
        dst, src = buf[off], buf[off + 1]      # object bytes: dest bank, src bank
        return "$%02X,$%02X" % (src, dst), None
    raise KeyError(mode)


def decode(buf, off, bank, addr, m8, x8):
    """Decode one instruction at buf[off], executing as CPU address (bank,addr).

    Returns (instruction_dict, new_m8, new_x8). The dict carries: bank, addr, len,
    raw (bytes), mnem, mode, text, target ((bank,addr) or None), note ('m8'/'m16'/
    'x8'/'x16' for immediates), m8, x8 (the widths in effect for THIS instruction).
    Width updates (REP/SEP) are reflected in the returned new_m8/new_x8.
    """
    opcode = buf[off]
    mnem, mode = OPCODES[opcode]
    olen = operand_len(mode, m8, x8)
    ilen = 1 + olen
    next_addr = (addr + ilen) & 0xFFFF
    text, target = _format_operand(mode, buf, off + 1, olen, bank, next_addr)

    note = ""
    if mode == "imm_m":
        note = "m8" if m8 else "m16"
    elif mode == "imm_x":
        note = "x8" if x8 else "x16"

    ins = {
        "bank": bank, "addr": addr, "len": ilen,
        "raw": bytes(buf[off:off + ilen]),
        "mnem": mnem, "mode": mode, "text": text,
        "target": target, "note": note, "m8": m8, "x8": x8,
    }

    nm8, nx8 = m8, x8
    if mnem == "REP":
        val = buf[off + 1]
        if val & 0x20:
            nm8 = False
        if val & 0x10:
            nx8 = False
    elif mnem == "SEP":
        val = buf[off + 1]
        if val & 0x20:
            nm8 = True
        if val & 0x10:
            nx8 = True
    # XCE into emulation forces 8-bit; carry isn't tracked here, so a driver that
    # cares can reset widths on `SEC/XCE`. REP/SEP cover the common native path.
    return ins, nm8, nx8


def render(ins):
    """One-line text form of a decoded instruction."""
    raw = " ".join("%02X" % b for b in ins["raw"])
    line = ("$%02X:%04X  %-11s %s %s"
            % (ins["bank"], ins["addr"], raw, ins["mnem"], ins["text"])).rstrip()
    tail = []
    if ins["note"]:
        tail.append(ins["note"])
    if ins["target"] is not None and ins["mnem"] in BRANCHES:
        tail.append("-> $%02X:%04X" % ins["target"])
    if tail:
        line += "   ; " + " ".join(tail)
    return line


def selftest():
    assert len(OPCODES) == 256, "need 256 opcodes, have %d" % len(OPCODES)
    for i, (mn, md) in enumerate(OPCODES):
        assert md in OPERAND_BYTES, "opcode %02X unknown mode %r" % (i, md)
        assert 1 <= len(mn) <= 3, "opcode %02X bad mnemonic %r" % (i, mn)
    # spot-checks cross-verified against 6502.org 65c816opcodes
    checks = {0x00: ("BRK", "imm8"), 0x18: ("CLC", "imp"), 0x1B: ("TCS", "imp"),
              0x20: ("JSR", "abs"), 0x22: ("JSL", "abl"), 0x40: ("RTI", "imp"),
              0x44: ("MVP", "bm"), 0x54: ("MVN", "bm"), 0x5C: ("JML", "abl"),
              0x60: ("RTS", "imp"), 0x62: ("PER", "rell"), 0x6C: ("JMP", "ind"),
              0x78: ("SEI", "imp"), 0x7C: ("JMP", "iax"), 0x80: ("BRA", "rel"),
              0x82: ("BRL", "rell"), 0xA2: ("LDX", "imm_x"), 0xA9: ("LDA", "imm_m"),
              0xC2: ("REP", "imm8"), 0xD4: ("PEI", "pei"), 0xDC: ("JML", "ial"),
              0xE2: ("SEP", "imm8"), 0xF4: ("PEA", "pea"), 0xFB: ("XCE", "imp"),
              0xFC: ("JSR", "iax")}
    for op, exp in checks.items():
        assert OPCODES[op] == exp, "opcode %02X = %r, expected %r" % (op, OPCODES[op], exp)
    # dynamic immediate length honors the width flags
    assert decode(bytes([0xA9, 0x34, 0x12]), 0, 0, 0x8000, True, True)[0]["len"] == 2   # LDA #$34 (m8)
    assert decode(bytes([0xA9, 0x34, 0x12]), 0, 0, 0x8000, False, True)[0]["len"] == 3  # LDA #$1234 (m16)
    assert decode(bytes([0xA2, 0x34, 0x12]), 0, 0, 0x8000, True, False)[0]["len"] == 3  # LDX #$1234 (x16)
    # REP/SEP flip the tracked widths
    _, m8, x8 = decode(bytes([0xC2, 0x30]), 0, 0, 0x8000, True, True)      # REP #$30
    assert (m8, x8) == (False, False)
    _, m8, x8 = decode(bytes([0xE2, 0x20]), 0, 0, 0x8000, False, False)    # SEP #$20
    assert (m8, x8) == (True, False)
    print("w65c816 selftest OK: 256 opcodes, modes valid, spot-checks + M/X length + REP/SEP pass")


if __name__ == "__main__":
    selftest()
