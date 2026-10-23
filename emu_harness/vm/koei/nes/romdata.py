#!/usr/bin/env python3
"""
Read a KOEI NES title's ROM data by (bank, address) for the data walk (row M of MIGRATION.md).

  peek     koei-nes / ro3k dump_data: N consecutive NUL-terminated strings, a hex dump, or a table of
           16-bit pointers with the string each one hits. Offsets come from the window model, so an
           8K MMC5 title reads the right bytes (dump_data's `bank*0x4000 + (cpu & 0x3FFF)` was right
           for 16K MMC1 titles only), and a pointer into the fixed window reads the fixed bank.
  records  a fixed-stride record table from the title profile, `[<title>.records.<name>]`:
             bank, addr (the CPU address the code reads it at), window (that window's base),
             stride, count, layout ("lead_name": a leading printable name, then the bytes)
           From bk dump_records.

    py -3.14 -m emu_harness.vm.koei.nes.romdata <title> peek <bank> <0xADDR> --str [N] | --bytes N | --ptrs N
    py -3.14 -m emu_harness.vm.koei.nes.romdata <title> records [<name> [N]]
"""
import sys

from emu_harness.vm.koei.nes import titles


class Rom:
    def __init__(self, title):
        self.cfg = cfg = titles.game_cfg(title)
        rom = cfg["_rom"].read_bytes()
        self.prg = rom[16:16 + rom[4] * 0x4000]
        self.win = titles.windows(cfg)
        self.size = cfg.get("bank_size", 0x4000)

    def off(self, bank, addr):
        """PRG offset of `addr` seen from `bank`: its own window, else the fixed / pinned bank there."""
        if not self.win.fits(bank, addr):
            for b, base in self.win.fixed().items():
                if base <= addr < base + self.size:
                    return self.win.prg(b, addr)
        return self.win.prg(bank, addr)

    def read_str(self, off):
        s = bytearray()
        while off < len(self.prg) and self.prg[off] not in (0, 0xFF) and len(s) < 64:
            b = self.prg[off]; s.append(b if 32 <= b < 127 else ord('.')); off += 1
        return s.decode('latin1'), off


def cmd_peek(R, bank, cpu, mode, n):
    PRG, base = R.prg, R.off(bank, cpu)
    if mode == "--str":
        off = base
        for _ in range(n if n is not None else 8):
            while off < len(PRG) and PRG[off] == 0:
                off += 1
            s, end = R.read_str(off)
            if not s:
                break
            print(f"  ${(cpu + (off - base)) & 0xFFFF:04X}: {s!r}")
            off = end
    elif mode == "--bytes":
        chunk = PRG[base:base + n]
        for i in range(0, len(chunk), 16):
            row = chunk[i:i + 16]
            hexs = " ".join(f"{b:02X}" for b in row)
            asci = "".join(chr(b) if 32 <= b < 127 else '.' for b in row)
            print(f"  ${(cpu + i) & 0xFFFF:04X}: {hexs:<48} {asci}")
    elif mode == "--ptrs":
        for i in range(n):
            p = PRG[base + i * 2] | (PRG[base + i * 2 + 1] << 8)
            s, _ = R.read_str(R.off(bank, p)) if p >= 0x8000 else ("", 0)
            print(f"  [{i}] -> ${p:04X}  {s!r}")


def cmd_records(R, which, n):
    tables = R.cfg.get("records", {})
    if which is None:
        for name, t in tables.items():
            print(f"  {name}: bank {t['bank']} ${t['addr']:04X} (window ${t['window']:04X}), stride {t['stride']}, count {t['count']}")
        return
    t = tables[which]
    base = t["bank"] * R.size + (t["addr"] - t["window"])
    stride, count = t["stride"], t["count"]
    print(f"bank-{t['bank']} {which} records: base ${t['addr']:04X}, stride {stride}, count {count}")
    for i in range(min(n, count)):
        r = R.prg[base + i * stride: base + i * stride + stride]
        name, j = "", 0
        while j < stride and 32 <= r[j] < 127:
            name += chr(r[j]); j += 1
        tail = " ".join(f"{c:3d}" for c in r[j:])
        print(f"{i:>3} '{name}'  | [{j}..]: {tail}")


def main(argv):
    if len(argv) < 3 or argv[2] not in ("peek", "records"):
        sys.exit(__doc__)
    R = Rom(argv[1])
    if argv[2] == "peek":
        bank, cpu, mode = int(argv[3], 0), int(argv[4], 0), argv[5]
        cmd_peek(R, bank, cpu, mode, int(argv[6]) if len(argv) > 6 else None)
    else:
        cmd_records(R, argv[3] if len(argv) > 3 else None, int(argv[4]) if len(argv) > 4 else 14)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
