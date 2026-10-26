#!/usr/bin/env python3
"""
chr_extract.py - KOEI CHR-RAM acquisition layer (reconstruct CHR planes from a Mesen trace).

This is the "ACQUIRE planes" front-end of the KOEI asset pipeline -- the step that comes BEFORE
machines.nes.ppu. KOEI Famicom titles ship CHR-RAM, not CHR-ROM: the pattern tables are empty at power-on
and the game uploads tile bitmaps into PPU $0000-$1FFF at runtime via its bulk CHR uploader. So you
can't just slice tiles out of the ROM the way portrait.py / text.py do for ROM-resident glyph blocks;
for the runtime-uploaded sheets (title screen, battle sprites, ...) you have to WATCH the upload
happen. This tool replays a Mesen CPU instruction trace and reconstructs the resulting CHR-RAM blob.

Output: a CHR-RAM byte blob (one PPU window, default $1000-$1FFF = 4 KB). That blob is the INPUT to
`machines.nes.ppu` -- e.g. `ppu.render_chr_atlas(blob, count=256, cols=16)` to eyeball the sheet, or
feed it as the `portrait_chr` / font-CHR argument the renderer modules expect. This module does NOT
import machines.nes.ppu: it produces planes, it does not draw them. Dependency direction is one-way.

How the upload is observed (the KOEI-specific part, hence config-driven):
  The bulk uploader sets the PPU write address (PPUADDR $2006) from two zero-page bytes, then streams
  tile bytes one-by-one through PPUDATA ($2007). In NA1 the loop body lives at three fixed PCs:
    upload_hi  ($C293) - LDA <zp_hi>   : high byte of the PPU dest address
    upload_lo  ($C299) - LDA <zp_lo>   : low byte; the very next store latches PPUADDR
    upload_byte($C2A1) - STA $2007     : one CHR byte; the written value is the A register
  Reading the trace in PC order and replaying these three events rebuilds the full pattern table.
  A new title supplies its OWN three handler PCs (and which PPU window to keep) via a config block --
  the trace-parsing logic below is title-independent.

Config is per-title (NA1 here; a new title with a runtime CHR uploader adds its own block). The trace
regexes match Mesen's default trace-logger line format (PC at col 0, `A:XX`, `= $XX`).
"""
import sys, re
from pathlib import Path

# ---- per-title CHR-upload config ----
# upload_hi / upload_lo / upload_byte : the three PC addresses of the bulk-uploader loop body.
#   - at upload_hi  the trace line carries `= $XX`, the high byte of the PPU dest address
#   - at upload_lo  the trace line carries `= $XX`, the low byte  (PPU addr = hi<<8 | lo)
#   - at upload_byte the trace line carries `A:XX`, the CHR byte streamed to PPUDATA
# keep_window : (lo, hi) PPU range to emit (NA1's title/battle sheets live at $1000-$1FFF).
# ppu_size    : total PPU address space tracked (CHR-RAM is $0000-$1FFF; wrap mask is ppu_wrap).
NA1 = {
    "upload_hi":   0xC293,
    "upload_lo":   0xC299,
    "upload_byte": 0xC2A1,
    "keep_window": (0x1000, 0x2000),
    "ppu_size":    0x2000,
    "ppu_wrap":    0x3FFF,
}

_TITLES = {"na1": NA1}

PC_RE = re.compile(r"^([0-9A-F]{4}) ")
A_RE  = re.compile(r"\bA:([0-9A-F]{2})")
EQ_RE = re.compile(r"= \$([0-9A-F]{2})")


def _open_trace(path):
    """Open a trace, transparently handling .gz (Mesen trace dumps are often gzipped)."""
    path = Path(path)
    if path.suffix == ".gz":
        import gzip
        return gzip.open(path, "rt", encoding="latin-1")
    return open(path, encoding="latin-1")


def reconstruct_chr(trace_path, cfg=NA1):
    """Replay a Mesen trace's PPU-upload events into a CHR-RAM blob.

    Returns (blob, written) where `blob` is the bytes of cfg['keep_window'] reconstructed from the
    trace, and `written` is the count of non-zero bytes in that window (a quick "did it fill?" check).
    The parsing logic is title-independent; the three handler PCs come from `cfg`."""
    hi_pc   = f"{cfg['upload_hi']:04X}"
    lo_pc   = f"{cfg['upload_lo']:04X}"
    byte_pc = f"{cfg['upload_byte']:04X}"
    wrap    = cfg["ppu_wrap"]
    size    = cfg["ppu_size"]

    ppu_hi = ppu_lo = ppu_addr = 0
    chr_data = bytearray(size)               # PPU $0000-$1FFF (CHR-RAM range we care about)

    with _open_trace(trace_path) as f:
        for line in f:
            pc_m = PC_RE.match(line)
            if not pc_m:
                continue
            pc = pc_m.group(1)

            if pc == hi_pc:                  # high byte of PPU dest
                m = EQ_RE.search(line)
                if m:
                    ppu_hi = int(m.group(1), 16)
            elif pc == lo_pc:                # low byte; next store latches PPUADDR
                m = EQ_RE.search(line)
                if m:
                    ppu_lo = int(m.group(1), 16)
                    ppu_addr = (ppu_hi << 8) | ppu_lo
            elif pc == byte_pc:              # one CHR byte (the A register)
                a_m = A_RE.search(line)
                if a_m:
                    byte = int(a_m.group(1), 16)
                    if ppu_addr < size:      # only keep the CHR-RAM range
                        chr_data[ppu_addr] = byte
                    ppu_addr = (ppu_addr + 1) & wrap

    lo, hi = cfg["keep_window"]
    blob = bytes(chr_data[lo:hi])
    written = sum(1 for b in blob if b != 0)
    return blob, written


def extract(trace_path, out, *, game="na1", cfg=None):
    """Reconstruct CHR-RAM from `trace_path` and write the kept window to `out` (a .bin the renderer reads)."""
    cfg = cfg or _TITLES[game]
    blob, written = reconstruct_chr(trace_path, cfg)
    Path(out).write_bytes(blob)
    lo, hi = cfg["keep_window"]
    print(f"wrote {out}  ({len(blob)} bytes from PPU ${lo:04X}-${hi - 1:04X} reconstructed from trace)")
    print(f"  {written} of {len(blob)} bytes are non-zero")
    return blob


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    trace = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "title-chr.bin"
    game = sys.argv[3] if len(sys.argv) > 3 else "na1"
    extract(trace, out, game=game)
