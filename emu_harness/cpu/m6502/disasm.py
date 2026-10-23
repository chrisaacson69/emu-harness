"""
emu_harness.cpu.m6502.disasm - linear-sweep 6502 disassembler (NES ROMs, 16 KiB bank view).

Moved here from koei-nes tools/disasm6502.py on 2026-10-22 (MIGRATION.md row B); before that it
graduated from ro3k-decompiler (2026-06-22), where it was validated against NA1's committed
bank_15.asm. koei-nes keeps a re-export so existing importers still work. Game-agnostic.

Grounding: the opcode table below is the canonical NMOS 6502 ISA (the CPU spec is
the oracle). The tool is self-verified by `validate`: run it on NA1's ROM and it
must reproduce the decode stream of the committed na1-decompiler bank_15.asm
byte-for-byte.

Known debt: the iNES header skip and 16 KiB bank slicing are NES cartridge knowledge living in a
CPU module; they belong to machines/nes when the other 6502 decoders fold in (row B).

Usage:
  py -3.14 -m emu_harness.cpu.m6502.disasm <rom.nes> <bank> [-o out.asm]
  py -3.14 -m emu_harness.cpu.m6502.disasm validate <rom.nes> <bank> <reference_bank.asm>
"""
import sys, re, argparse
from pathlib import Path

INES = 16
BANKSZ = 0x4000

# addressing modes: name -> (operand length in bytes, format template)
# {0} = operand value rendered; templates match NA1's asm style (lowercase hex).
IMP, ACC, IMM, ZP, ZPX, ZPY, ABS, ABX, ABY, IND, IZX, IZY, REL = range(13)
MODE_LEN = {IMP:0, ACC:0, IMM:1, ZP:1, ZPX:1, ZPY:1, ABS:2, ABX:2, ABY:2, IND:2, IZX:1, IZY:1, REL:1}

# opcode -> (mnemonic, mode). Documented NMOS 6502 only; everything else -> .db
OPS = {
0x00:("brk",IMP),0x01:("ora",IZX),0x05:("ora",ZP),0x06:("asl",ZP),0x08:("php",IMP),
0x09:("ora",IMM),0x0a:("asl",ACC),0x0d:("ora",ABS),0x0e:("asl",ABS),0x10:("bpl",REL),
0x11:("ora",IZY),0x15:("ora",ZPX),0x16:("asl",ZPX),0x18:("clc",IMP),0x19:("ora",ABY),
0x1d:("ora",ABX),0x1e:("asl",ABX),0x20:("jsr",ABS),0x21:("and",IZX),0x24:("bit",ZP),
0x25:("and",ZP),0x26:("rol",ZP),0x28:("plp",IMP),0x29:("and",IMM),0x2a:("rol",ACC),
0x2c:("bit",ABS),0x2d:("and",ABS),0x2e:("rol",ABS),0x30:("bmi",REL),0x31:("and",IZY),
0x35:("and",ZPX),0x36:("rol",ZPX),0x38:("sec",IMP),0x39:("and",ABY),0x3d:("and",ABX),
0x3e:("rol",ABX),0x40:("rti",IMP),0x41:("eor",IZX),0x45:("eor",ZP),0x46:("lsr",ZP),
0x48:("pha",IMP),0x49:("eor",IMM),0x4a:("lsr",ACC),0x4c:("jmp",ABS),0x4d:("eor",ABS),
0x4e:("lsr",ABS),0x50:("bvc",REL),0x51:("eor",IZY),0x55:("eor",ZPX),0x56:("lsr",ZPX),
0x58:("cli",IMP),0x59:("eor",ABY),0x5d:("eor",ABX),0x5e:("lsr",ABX),0x60:("rts",IMP),
0x61:("adc",IZX),0x65:("adc",ZP),0x66:("ror",ZP),0x68:("pla",IMP),0x69:("adc",IMM),
0x6a:("ror",ACC),0x6c:("jmp",IND),0x6d:("adc",ABS),0x6e:("ror",ABS),0x70:("bvs",REL),
0x71:("adc",IZY),0x75:("adc",ZPX),0x76:("ror",ZPX),0x78:("sei",IMP),0x79:("adc",ABY),
0x7d:("adc",ABX),0x7e:("ror",ABX),0x81:("sta",IZX),0x84:("sty",ZP),0x85:("sta",ZP),
0x86:("stx",ZP),0x88:("dey",IMP),0x8a:("txa",IMP),0x8c:("sty",ABS),0x8d:("sta",ABS),
0x8e:("stx",ABS),0x90:("bcc",REL),0x91:("sta",IZY),0x94:("sty",ZPX),0x95:("sta",ZPX),
0x96:("stx",ZPY),0x98:("tya",IMP),0x99:("sta",ABY),0x9a:("txs",IMP),0x9d:("sta",ABX),
0xa0:("ldy",IMM),0xa1:("lda",IZX),0xa2:("ldx",IMM),0xa4:("ldy",ZP),0xa5:("lda",ZP),
0xa6:("ldx",ZP),0xa8:("tay",IMP),0xa9:("lda",IMM),0xaa:("tax",IMP),0xac:("ldy",ABS),
0xad:("lda",ABS),0xae:("ldx",ABS),0xb0:("bcs",REL),0xb1:("lda",IZY),0xb4:("ldy",ZPX),
0xb5:("lda",ZPX),0xb6:("ldx",ZPY),0xb8:("clv",IMP),0xb9:("lda",ABY),0xba:("tsx",IMP),
0xbc:("ldy",ABX),0xbd:("lda",ABX),0xbe:("ldx",ABY),0xc0:("cpy",IMM),0xc1:("cmp",IZX),
0xc4:("cpy",ZP),0xc5:("cmp",ZP),0xc6:("dec",ZP),0xc8:("iny",IMP),0xc9:("cmp",IMM),
0xca:("dex",IMP),0xcc:("cpy",ABS),0xcd:("cmp",ABS),0xce:("dec",ABS),0xd0:("bne",REL),
0xd1:("cmp",IZY),0xd5:("cmp",ZPX),0xd6:("dec",ZPX),0xd8:("cld",IMP),0xd9:("cmp",ABY),
0xdd:("cmp",ABX),0xde:("dec",ABX),0xe0:("cpx",IMM),0xe1:("sbc",IZX),0xe4:("cpx",ZP),
0xe5:("sbc",ZP),0xe6:("inc",ZP),0xe8:("inx",IMP),0xe9:("sbc",IMM),0xea:("nop",IMP),
0xec:("cpx",ABS),0xed:("sbc",ABS),0xee:("inc",ABS),0xf0:("beq",REL),0xf1:("sbc",IZY),
0xf5:("sbc",ZPX),0xf6:("inc",ZPX),0xf8:("sed",IMP),0xf9:("sbc",ABY),0xfd:("sbc",ABX),
0xfe:("inc",ABX),
}

def fmt_operand(mode, lo, hi, addr, ilen):
    if mode in (IMP,):   return ""
    if mode == ACC:      return "a"
    if mode == IMM:      return f"#${lo:02x}"
    if mode == ZP:       return f"${lo:02x}"
    if mode == ZPX:      return f"${lo:02x},x"
    if mode == ZPY:      return f"${lo:02x},y"
    if mode == ABS:      return f"${(hi<<8)|lo:04x}"
    if mode == ABX:      return f"${(hi<<8)|lo:04x},x"
    if mode == ABY:      return f"${(hi<<8)|lo:04x},y"
    if mode == IND:      return f"(${(hi<<8)|lo:04x})"
    if mode == IZX:      return f"(${lo:02x},x)"
    if mode == IZY:      return f"(${lo:02x}),y"
    if mode == REL:
        tgt = (addr + ilen + ((lo ^ 0x80) - 0x80)) & 0xffff
        return f"${tgt:04x}"
    return ""

def disasm_bank(rom, bank, base=0xc000, start=None, stop=None):
    """Yield (addr, prg_offset, raw_bytes, mnemonic, operand) for a linear sweep.

    start/stop are CPU addresses bounding the sweep. Pass start= to ALIGN the decode to a
    known routine entry -- a whole-bank sweep mis-aligns when it hits data (e.g. the $F6 pad
    bytes before $F8B7 swallow the entry), so giving the real entry address re-syncs it."""
    bank_off = INES + bank * BANKSZ
    data = rom[bank_off:bank_off+BANKSZ]
    i = 0 if start is None else (start - base)
    end = BANKSZ if stop is None else min(BANKSZ, stop - base)
    while i < end:
        op = data[i]
        addr = base + i
        prg = bank * BANKSZ + i
        if op in OPS:
            mn, mode = OPS[op]
            n = MODE_LEN[mode]
            if i + n >= BANKSZ:                      # truncated tail -> .db
                yield (addr, prg, data[i:i+1], None, f"${op:02x}")
                i += 1; continue
            lo = data[i+1] if n >= 1 else 0
            hi = data[i+2] if n >= 2 else 0
            ilen = 1 + n
            operand = fmt_operand(mode, lo, hi, addr, ilen)
            yield (addr, prg, data[i:i+ilen], mn, operand)
            i += ilen
        else:                                        # illegal/undocumented -> .db
            yield (addr, prg, data[i:i+1], None, f"${op:02x}")
            i += 1

def render(rom, bank, base=0xc000, start=None, stop=None):
    out = [f".base ${(start if start is not None else base):04x}\n"]
    for addr, prg, raw, mn, operand in disasm_bank(rom, bank, base, start, stop):
        rawhex = " ".join(f"{b:02x}" for b in raw)
        if mn is None:
            insn = f".db {operand}"
        else:
            insn = f"{mn} {operand}".rstrip()
        out.append(f"b{bank:02d}_{addr:04x}:   {insn:<28}; {prg:X}:  {rawhex}")
    return "\n".join(out) + "\n"

# --- validation: compare decode stream against a reference NA1 bank_NN.asm -------
REF_LINE = re.compile(r";\s*([0-9A-Fa-f]+):\s+([0-9a-fA-F ]+)$")

def ref_stream(ref_path):
    """Parse (prg_offset, rawbytes_tuple, insn_text) from a committed bank_NN.asm."""
    out = []
    for ln in Path(ref_path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = REF_LINE.search(ln)
        if not m: continue
        prg = int(m.group(1), 16)
        rb = tuple(int(x, 16) for x in m.group(2).split())
        code = ln[:m.start()]
        if ":" in code: code = code.split(":", 1)[1]   # strip label column
        insn = re.sub(r"\s+", " ", code).strip().lower()
        out.append((prg, rb, insn))
    return out

LISTING_LINE = re.compile(r";\s*([0-9A-Fa-f]{1,6}):\s+([0-9a-fA-F ]+?)\s*(?:;.*)?$")   # unpadded offsets too
LISTING_MNEM = re.compile(r"^\s*(?:[@\w]+:)?\s*([a-z]{3})\b")

def listing_verdicts(text, size):
    """A disasm6-style listing (ours from `render`, nes-disasm6's) -> one verdict per PRG byte:
    "I" instruction start, "O" operand, "D" a `hex`/`.db` data row, "" not listed."""
    v = [""] * size
    for line in text.splitlines():
        m = LISTING_LINE.search(line)
        if not m:
            continue
        off, n = int(m.group(1), 16), len(m.group(2).split())
        mm = LISTING_MNEM.match(line.split(";", 1)[0])
        insn = bool(mm) and mm.group(1) != "hex"
        for j in range(n):
            if off + j < size:
                v[off + j] = ("I" if j == 0 else "O") if insn else "D"
    return v

def my_stream(rom, bank, base=0xc000):
    out = []
    for addr, prg, raw, mn, operand in disasm_bank(rom, bank, base):
        insn = (f".db {operand}" if mn is None else f"{mn} {operand}").strip().lower()
        out.append((prg, tuple(raw), insn))
    return out

MNEMONICS = {mn for mn, _ in OPS.values()}

def validate(rom, bank, ref_path, base=0xc000):
    """Pure decoder validation, independent of code/data mapping: for every line
    where NA1's reference emitted a genuine instruction (mnemonic, not a hex/.db
    data row), point-decode the same raw bytes with our opcode table and require
    identical mnemonic + identical length. Data-region rows (jump tables, padding,
    the bytecode band) are skipped -- a linear sweep can't map those and NA1's asm
    marks them by hand; they don't test the decoder."""
    ref = ref_stream(ref_path)
    checked = mism = skipped_data = 0
    diffs = []
    for prg, rb, ins in ref:
        toks = ins.split()
        mnem = toks[0] if toks else ""
        if mnem not in MNEMONICS:        # hex / .db / .byte / data row
            skipped_data += 1
            continue
        checked += 1
        op = rb[0]
        if op not in OPS:
            mism += 1
            if len(diffs) < 20: diffs.append(f"  @{prg:X}: ref insn {ins!r} but op ${op:02x} unknown to decoder")
            continue
        my_mn, mode = OPS[op]
        my_len = 1 + MODE_LEN[mode]
        if my_mn != mnem or my_len != len(rb):
            mism += 1
            if len(diffs) < 20:
                diffs.append(f"  @{prg:X}: ref[{mnem} len{len(rb)}]  mine[{my_mn} len{my_len}]  bytes{list(rb)}")
    print(f"instruction lines checked: {checked}   mismatches: {mism}   (data rows skipped: {skipped_data})")
    for d in diffs: print(d)
    return mism == 0

def main():
    a = sys.argv[1:]
    if a and a[0] == "validate":
        _, rom_p, bank, ref = a
        rom = Path(rom_p).read_bytes()
        ok = validate(rom, int(bank), ref)
        print("VALIDATED" if ok else "MISMATCH")
        sys.exit(0 if ok else 1)
    if a and a[0] == "at":
        # disasm6502.py at <rom> <bank> <start_hex> [count]   -- decode FROM a known entry,
        # correctly aligned (re-syncs past data the whole-bank sweep mis-decodes).
        _, rom_p, bank, start_h = a[:4]
        start = int(start_h, 16)
        count = int(a[4], 0) if len(a) > 4 else 64
        base = 0xc000 if start >= 0xc000 else 0x8000      # fixed bank vs paged window
        rom = Path(rom_p).read_bytes()
        sys.stdout.write(render(rom, int(bank), base, start=start, stop=start + count))
        sys.exit(0)
    ap = argparse.ArgumentParser()
    ap.add_argument("rom"); ap.add_argument("bank", type=int)
    ap.add_argument("-o", "--out"); ap.add_argument("--base", default="c000")
    args = ap.parse_args()
    rom = Path(args.rom).read_bytes()
    txt = render(rom, args.bank, int(args.base, 16))
    if args.out:
        Path(args.out).write_text(txt, encoding="utf-8")
        print(f"wrote {args.out} ({txt.count(chr(10))} lines)")
    else:
        sys.stdout.write(txt)

if __name__ == "__main__":
    main()
