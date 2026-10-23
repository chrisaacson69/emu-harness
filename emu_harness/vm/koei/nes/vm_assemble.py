"""Sea-16 VM ENCODER — the inverse of vm-disasm.py's decode (Pass 2, Layer 1).

Built 2026-06-03 for the VM-validation round-trip. `vm-disasm.py` turns ROM bytes
into instructions; this turns an instruction back into bytes. The pair gives us a
ROUND-TRIP proof: for every instruction the disassembler decodes, re-encoding from
the *structured* operand values must reproduce the exact ROM bytes.

WHY THIS PROVES SOMETHING (not a tautology):
  encode_instr does NOT echo the raw bytes. It DECODES each operand into a value
  (LE word, signed frame offset, switch table of (key,target) pairs, LE32 inline
  immediate, adj byte) and then RE-ENCODES that value back to bytes. The round-trip
  passing means decode-then-encode is the identity, which only holds if our field
  LAYOUT and ENDIANNESS model is correct:
    * a big-endian misread of a word would re-split to swapped bytes  -> caught
    * a wrong switch-table stride/field order would mis-place bytes   -> caught
    * a wrong instruction length desyncs coverage vs ROM ONLY when the misread bytes do not
      happen to re-encode as themselves. Measured 2026-10-23 (emu-harness MIGRATION.md row F3):
      the pre-F2 widths of $B7 $0C-$13 (operand 0 instead of 2) still round-trip CLEAN on ro3k,
      rot3k2 and UW, because each operand byte decodes as a 1-byte opcode. So the round trip
      proves encode/decode CONSISTENCY, layout and endianness, not the opcode lengths; those
      are measured on each cartridge's own handlers (emu-harness tools/koei_isa_probe.py).
  So a clean round-trip = the operand encoding (layout, endianness, the switch tables, the
  per-title stub prologue) is consistent with the decoder; lengths need the handler probe.
  This is "Rung 1" of validating the VM; the
  emulator stack-effect audit (Rung 2, vm-stack-audit.py) proves MEANING.

Length authority: vm-disasm.instr_len (which itself reads nobunaga_vm.OPCODE_INFO
with the handler-verified overrides). We deliberately reuse it so the encoder and
disassembler can never disagree on length.

Usage (library): from vm_assemble import encode_instr, encode_sub, encode_prologue
CLI self-test:    py -3 tools/vm_assemble.py <bank>   # encode every sub, report bytes
"""

import importlib.util
import sys
from pathlib import Path

from emu_harness.vm.koei.isa import TABLE as ISA_TABLE
from emu_harness.vm.koei.nes import vm_disasm    # reuse its decode authority

instr_len = vm_disasm.instr_len
walk_sub = vm_disasm.walk_sub
find_stubs = vm_disasm.find_stubs
frame_off = vm_disasm.frame_off
bank_range = vm_disasm.bank_range
VM_ENTRY_STUB = vm_disasm.VM_ENTRY_STUB     # b"\x20\x23\xE8" = JSR $E823


def _w(v):
    """Pack a 16-bit value little-endian (the VM's word encoding)."""
    return [v & 0xFF, (v >> 8) & 0xFF]


def _le32(v):
    """Pack a 32-bit value little-endian (B7 $18/$19 inline immediate)."""
    return [v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF]


def encode_prologue(mem, stub):
    """Re-encode the 5-byte subroutine stub `JSR <vm_entry> <frame:2>` from the decoded
    SIGNED frame offset. Proves the signed-word frame encoding round-trips. The JSR is the
    title's own (vm_disasm.VM_ENTRY_STUB, set per title; NA1's is `20 23 E8`)."""
    fo = frame_off(mem, stub)            # signed 16-bit
    v = fo & 0xFFFF
    return bytes(vm_disasm.VM_ENTRY_STUB) + bytes(_w(v))


def encode_instr(mem, pc):
    """Re-encode the single VM instruction at `pc` from its decoded operand values.

    Returns the bytes the instruction occupies. Compared against the ROM slice by
    the round-trip harness; equality is the proof (see module docstring)."""
    op = mem.read(pc)
    ln = instr_len(mem, pc)
    rd = mem.read

    # --- Structured / variable-length operands (the genuinely non-trivial cases) ---
    if op == 0xD5:                                   # SWITCH_contig: offs,limit,default + limit targets
        offs = rd(pc + 1) | (rd(pc + 2) << 8)
        limit = rd(pc + 3) | (rd(pc + 4) << 8)
        default = rd(pc + 5) | (rd(pc + 6) << 8)
        cases = [rd(pc + 7 + 2 * k) | (rd(pc + 8 + 2 * k) << 8) for k in range(limit)]
        out = [op] + _w(offs) + _w(limit) + _w(default)
        for c in cases:
            out += _w(c)
        return bytes(out)

    if op == 0xD9:                                   # SWITCH_noncontig: count + (key,target)*count + default
        count = rd(pc + 1) | (rd(pc + 2) << 8)
        out = [op] + _w(count)
        base = pc + 3
        for k in range(count):
            key = rd(base + 4 * k) | (rd(base + 4 * k + 1) << 8)
            tgt = rd(base + 4 * k + 2) | (rd(base + 4 * k + 3) << 8)
            out += _w(key) + _w(tgt)
        default = rd(base + 4 * count) | (rd(base + 4 * count + 1) << 8)
        out += _w(default)
        return bytes(out)

    if op == 0xB7:                                   # LONG/ext_op: index byte + the ext op's operand
        idx = rd(pc + 1)                             # (its width from the shared table, MIGRATION.md F2)
        e = ISA_TABLE.extended.get(idx)
        width = e.size if e else 0
        if width == 4:                               # $18/$19 load_a32/b32_from_ptr3: LE32 immediate
            imm = (rd(pc + 2) | (rd(pc + 3) << 8)
                   | (rd(pc + 4) << 16) | (rd(pc + 5) << 24))
            return bytes([op, idx] + _le32(imm))
        if width == 2:                               # $0C-$13: a frame offset / absolute address
            return bytes([op, idx] + _w(rd(pc + 2) | (rd(pc + 3) << 8)))
        return bytes([op, idx])

    # --- Generic operands keyed on the authoritative length ---
    if ln == 1:                                      # opcode only (operand in low nibble for op<0x80)
        return bytes([op])
    if ln == 2:                                      # one operand byte (frame off / sbyte / imm1 / count)
        return bytes([op, rd(pc + 1)])
    if ln == 3:                                      # 16-bit operand (addr / word imm / jump target)
        word = rd(pc + 1) | (rd(pc + 2) << 8)
        return bytes([op] + _w(word))
    if ln == 4 and op == 0xE9:                       # CALL_abs_imm1: word target + adj byte
        tgt = rd(pc + 1) | (rd(pc + 2) << 8)
        adj = rd(pc + 3)
        return bytes([op] + _w(tgt) + [adj])

    # Unmodeled length/shape. Every real opcode is covered above (lengths 1-4 plus
    # the structured D5/D9/B7 cases), so reaching here means the model grew a hole.
    raise ValueError(f"unmodeled instruction op=${op:02X} len={ln} at ${pc:04X}")


def encode_sub(mem, stub, end):
    """Re-encode a whole bytecode subroutine: the 5-byte stub prologue followed by
    every instruction from body (stub+5) to the sub's terminator. Returns
    (encoded_bytes, final_pc) where final_pc is the first address past the sub."""
    out = bytearray(encode_prologue(mem, stub))
    body = stub + 5
    instrs, final_pc, _ = walk_sub(mem, body, end, {})
    for ins in instrs:
        out += encode_instr(mem, ins["addr"])
    return bytes(out), final_pc


def _main():
    import argparse
    from emu_harness.vm.koei.nes.nobunaga_vm import NobunagaVM
    ap = argparse.ArgumentParser(description="Sea-16 VM encoder self-test")
    ap.add_argument("bank", type=lambda x: int(x, 0), help="bank 0-15")
    args = ap.parse_args()
    vm = NobunagaVM()
    vm.switch_bank(args.bank)
    mem = vm.mem
    base, size = bank_range(args.bank)
    stubs = find_stubs(mem, base, size)
    total = 0
    for i, stub in enumerate(stubs):
        end = (stubs[i + 1] if i + 1 < len(stubs) else base + size)
        enc, final_pc = encode_sub(mem, stub, end)
        total += len(enc)
    print(f"bank {args.bank}: encoded {len(stubs)} subs, {total} bytes")


if __name__ == "__main__":
    _main()
