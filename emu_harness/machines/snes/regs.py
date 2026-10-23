#!/usr/bin/env python3
"""
SNES memory-mapped register names (system-agnostic reference).

Maps a 16-bit address to its hardware register name + subsystem, so a
disassembler/probe can say "this touches $2118 = VMDATAL (PPU/VRAM)" instead of a
bare number. Grounded in the SNESdev wiki register list. `name(addr)` returns
(name, subsystem) or (None, None).
"""

PPU = "PPU"
DMA = "DMA"
CPU = "CPU"
APU = "APU"
INPUT = "INPUT"
WRAM = "WRAM"

# Fixed single registers.
_FIXED = {
    # --- PPU $2100-$213F ---
    0x2100: ("INIDISP", PPU), 0x2101: ("OBSEL", PPU), 0x2102: ("OAMADDL", PPU),
    0x2103: ("OAMADDH", PPU), 0x2104: ("OAMDATA", PPU), 0x2105: ("BGMODE", PPU),
    0x2106: ("MOSAIC", PPU), 0x2107: ("BG1SC", PPU), 0x2108: ("BG2SC", PPU),
    0x2109: ("BG3SC", PPU), 0x210A: ("BG4SC", PPU), 0x210B: ("BG12NBA", PPU),
    0x210C: ("BG34NBA", PPU), 0x210D: ("BG1HOFS", PPU), 0x210E: ("BG1VOFS", PPU),
    0x210F: ("BG2HOFS", PPU), 0x2110: ("BG2VOFS", PPU), 0x2111: ("BG3HOFS", PPU),
    0x2112: ("BG3VOFS", PPU), 0x2113: ("BG4HOFS", PPU), 0x2114: ("BG4VOFS", PPU),
    0x2115: ("VMAIN", PPU), 0x2116: ("VMADDL", PPU), 0x2117: ("VMADDH", PPU),
    0x2118: ("VMDATAL", PPU), 0x2119: ("VMDATAH", PPU), 0x211A: ("M7SEL", PPU),
    0x211B: ("M7A", PPU), 0x211C: ("M7B", PPU), 0x211D: ("M7C", PPU),
    0x211E: ("M7D", PPU), 0x211F: ("M7X", PPU), 0x2120: ("M7Y", PPU),
    0x2121: ("CGADD", PPU), 0x2122: ("CGDATA", PPU), 0x2123: ("W12SEL", PPU),
    0x2124: ("W34SEL", PPU), 0x2125: ("WOBJSEL", PPU), 0x2126: ("WH0", PPU),
    0x2127: ("WH1", PPU), 0x2128: ("WH2", PPU), 0x2129: ("WH3", PPU),
    0x212A: ("WBGLOG", PPU), 0x212B: ("WOBJLOG", PPU), 0x212C: ("TM", PPU),
    0x212D: ("TS", PPU), 0x212E: ("TMW", PPU), 0x212F: ("TSW", PPU),
    0x2130: ("CGWSEL", PPU), 0x2131: ("CGADSUB", PPU), 0x2132: ("COLDATA", PPU),
    0x2133: ("SETINI", PPU), 0x2134: ("MPYL", PPU), 0x2135: ("MPYM", PPU),
    0x2136: ("MPYH", PPU), 0x2137: ("SLHV", PPU), 0x2138: ("OAMDATAREAD", PPU),
    0x2139: ("VMDATALREAD", PPU), 0x213A: ("VMDATAHREAD", PPU),
    0x213B: ("CGDATAREAD", PPU), 0x213C: ("OPHCT", PPU), 0x213D: ("OPVCT", PPU),
    0x213E: ("STAT77", PPU), 0x213F: ("STAT78", PPU),
    # --- APU I/O ports ---
    0x2140: ("APUIO0", APU), 0x2141: ("APUIO1", APU),
    0x2142: ("APUIO2", APU), 0x2143: ("APUIO3", APU),
    # --- WRAM access ports ---
    0x2180: ("WMDATA", WRAM), 0x2181: ("WMADDL", WRAM),
    0x2182: ("WMADDM", WRAM), 0x2183: ("WMADDH", WRAM),
    # --- old-style controller ports ---
    0x4016: ("JOYSER0", INPUT), 0x4017: ("JOYSER1", INPUT),
    # --- CPU $4200-$421F ---
    0x4200: ("NMITIMEN", CPU), 0x4201: ("WRIO", CPU), 0x4202: ("WRMPYA", CPU),
    0x4203: ("WRMPYB", CPU), 0x4204: ("WRDIVL", CPU), 0x4205: ("WRDIVH", CPU),
    0x4206: ("WRDIVB", CPU), 0x4207: ("HTIMEL", CPU), 0x4208: ("HTIMEH", CPU),
    0x4209: ("VTIMEL", CPU), 0x420A: ("VTIMEH", CPU), 0x420B: ("MDMAEN", DMA),
    0x420C: ("HDMAEN", DMA), 0x420D: ("MEMSEL", CPU), 0x4210: ("RDNMI", CPU),
    0x4211: ("TIMEUP", CPU), 0x4212: ("HVBJOY", CPU), 0x4213: ("RDIO", CPU),
    0x4214: ("RDDIVL", CPU), 0x4215: ("RDDIVH", CPU), 0x4216: ("RDMPYL", CPU),
    0x4217: ("RDMPYH", CPU), 0x4218: ("JOY1L", INPUT), 0x4219: ("JOY1H", INPUT),
    0x421A: ("JOY2L", INPUT), 0x421B: ("JOY2H", INPUT), 0x421C: ("JOY3L", INPUT),
    0x421D: ("JOY3H", INPUT), 0x421E: ("JOY4L", INPUT), 0x421F: ("JOY4H", INPUT),
}

_DMA_REG = {0x0: "DMAP", 0x1: "BBAD", 0x2: "A1TL", 0x3: "A1TH", 0x4: "A1B",
            0x5: "DASL", 0x6: "DASH", 0x7: "DASB", 0x8: "A2AL", 0x9: "A2AH",
            0xA: "NTRL"}


def name(addr):
    """(register_name, subsystem) for a 16-bit address, or (None, None)."""
    if addr in _FIXED:
        return _FIXED[addr]
    if 0x4300 <= addr <= 0x437F:          # DMA/HDMA channel registers
        ch = (addr >> 4) & 0x7
        reg = _DMA_REG.get(addr & 0xF)
        if reg:
            return ("%s%d" % (reg, ch), DMA)
    return (None, None)
