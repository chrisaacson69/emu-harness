"""VM encoding ROUND-TRIP harness — Rung 1 of validating the Sea-16 VM (Pass 2).

For every bytecode subroutine in the 4 code banks (0,1,2,15), re-encode it from the
DECODED structured operand values (vm_assemble.encode_sub) and assert the result is
BYTE-IDENTICAL to the ROM. A clean pass proves the opcode + operand-layout + length
+ endianness model is complete and correct (see vm_assemble.py docstring for why
this is not a tautology). This is the deterministic gate that anchors Pass 2: the
decompiler's view of the bytecode is only trustworthy once the bytes round-trip.

Coverage scope: the BYTECODE (every stub prologue + every instruction up to each
sub's terminator). Data tables embedded as switch operands are covered (they are
consumed inside the $D5/$D9 instructions). Inter-sub padding / pure data regions
are NOT VM code and are intentionally out of scope for Rung 1.

Usage:
  py -3 tools/vm-roundtrip.py            # all 4 code banks, summary + first diffs
  py -3 tools/vm-roundtrip.py 2          # one bank
  py -3 tools/vm-roundtrip.py --verbose  # list every sub's byte count
  py -3.14 -m emu_harness.vm.koei.nes.gates.roundtrip --game ro3k   # any title's code banks
"""

import argparse
import sys
from pathlib import Path

from emu_harness.vm.koei.nes.nobunaga_vm import NobunagaVM
from emu_harness.vm.koei.nes.vm_assemble import encode_sub, find_stubs, bank_range

CODE_BANKS = (0, 1, 2, 10, 14, 15)


def roundtrip_bank(bank, verbose=False, game=None):
    """Return (n_subs, total_bytes, mismatches) for one bank. mismatches is a list
    of (stub, final_pc, first_diff_offset, enc_byte, rom_byte). `game` = a title profile
    (games.toml), set up as the disassembler sees it (titles.bank_view); default NA1."""
    if game:
        from emu_harness.vm.koei.nes import titles
        vm, _VD, _cfg, base, size, stubs = titles.bank_view(game, bank)
        mem = vm.mem
    else:
        vm = NobunagaVM()
        vm.switch_bank(bank)
        mem = vm.mem
        base, size = bank_range(bank)
        stubs = find_stubs(mem, base, size)
    bounds = stubs + [base + size]

    total = 0
    mismatches = []
    for i, stub in enumerate(stubs):
        end = bounds[i + 1]
        enc, final_pc = encode_sub(mem, stub, end)
        span = final_pc - stub
        rom = bytes(mem.read(stub + j) for j in range(span))
        total += span
        if enc != rom:
            # locate the first differing byte for a precise report
            n = min(len(enc), len(rom))
            d = next((k for k in range(n) if enc[k] != rom[k]), n)
            eb = enc[d] if d < len(enc) else -1
            rb = rom[d] if d < len(rom) else -1
            mismatches.append((stub, final_pc, d, eb, rb))
        elif verbose:
            print(f"  ok  sub ${stub:04X}  {span} bytes")
    return len(stubs), total, mismatches


def main():
    ap = argparse.ArgumentParser(description="Sea-16 VM encoding round-trip (Rung 1)")
    ap.add_argument("bank", nargs="?", type=lambda x: int(x, 0),
                    help="single bank (default: all code banks 0,1,2,15)")
    ap.add_argument("--verbose", action="store_true", help="list every sub")
    ap.add_argument("--game", help="a title profile (games.toml); default NA1 and its code banks")
    args = ap.parse_args()
    if args.game:
        from emu_harness.vm.koei.nes import titles
        code_banks = titles.game_cfg(args.game)["code_banks"]
    else:
        code_banks = CODE_BANKS
    banks = [args.bank] if args.bank is not None else list(code_banks)

    grand_subs = grand_bytes = grand_bad = 0
    for bank in banks:
        n, total, mm = roundtrip_bank(bank, args.verbose, args.game)
        grand_subs += n
        grand_bytes += total
        grand_bad += len(mm)
        status = "PASS" if not mm else f"{len(mm)} MISMATCH"
        print(f"bank {bank:>2}: {n} subs | {total} code bytes re-encoded | {status}")
        for stub, final_pc, d, eb, rb in mm[:20]:
            print(f"    sub ${stub:04X} (->${final_pc:04X}): first diff at byte +{d} "
                  f"(${stub + d:04X}) enc=${eb & 0xFF:02X} rom=${rb & 0xFF:02X}")

    print("-" * 60)
    verdict = "ROUND-TRIP CLEAN" if grand_bad == 0 else f"{grand_bad} SUBS FAILED"
    print(f"TOTAL: {grand_subs} subs, {grand_bytes} bytes across {len(banks)} bank(s)"
          f"  ->  {verdict}")
    sys.exit(0 if grand_bad == 0 else 1)


if __name__ == "__main__":
    main()
