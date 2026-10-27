"""Live-emulator helpers for the NES (row P): the pieces every title's probe kept rewriting.

  play(m, script)          a readable input script: "wait*120,start,a*3,hold:left*60"
  PPUSnapshot.capture(m)   CHR / nametables as the PPU sees them (banking and mirroring applied),
                           palette RAM, OAM and PPU registers, at a frame boundary
  snap.page(i) / .frame()  a nametable page as a Canvas; the displayed frame rebuilt from it
  text(page, charmap)      a nametable page as text, through the game's font map

A run with a chosen battery save is `NES(rom, sram=...)`; the save Mesen held is put back
when it closes. Screenshots are `m.screenshot(path)`: the emulator's own rendered frame, never the desktop.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import NES, ppu
from .canvas import Canvas


def play(m: NES, script: str, *, press: int = 4, gap: int = 20):
    """Run an input script; yields each token after it ran, so a caller can inspect between steps.
    Tokens (comma separated): wait*N | btn[+btn][*N] (tap N times: hold `press`, release `gap`) |
    hold:btn[+btn]*N (hold N frames). Button names are the Mesen pad names (a b select start up
    down left right)."""
    for tok in filter(None, (t.strip() for t in script.split(","))):
        name, _, n = tok.partition("*")
        n = int(n or 1)
        if name == "wait":
            m.step(frames=n)
        elif name.startswith("hold:"):
            m.step(name[5:].split("+"), frames=n)
        else:
            for _ in range(n):
                m.step(name.split("+"), frames=press)
                m.step(frames=gap)
        yield tok


def run(m: NES, script: str, **kw) -> None:
    """play() to the end."""
    for _ in play(m, script, **kw):
        pass


@dataclass
class PPUSnapshot:
    chr: bytes          # $0000-$1FFF as the PPU sees it now (CHR banking applied)
    nametables: bytes   # $2000-$2FFF, the four logical pages (mirroring applied)
    palette: bytes      # 32 bytes of palette RAM
    oam: bytes          # 256 bytes
    regs: dict          # Mesen's ppu.* state

    @classmethod
    def capture(cls, m: NES) -> PPUSnapshot:
        return cls(m.read("nesPpuDebug", 0, 0x2000), m.read("nesPpuDebug", 0x2000, 0x1000),
                   m.read("nesPaletteRam", 0, 32), m.read("nesSpriteRam", 0, 256), m.state("ppu."))

    def _flag(self, key: str) -> bool:
        return self.regs.get(key) == "true"

    @property
    def bg_chr_base(self) -> int:
        return int(self.regs["ppu.control.backgroundPatternAddr"])

    @property
    def sprite_chr_base(self) -> int:
        return int(self.regs["ppu.control.spritePatternAddr"])

    @property
    def sprite_h(self) -> int:
        return 16 if self._flag("ppu.control.largeSprites") else 8

    def page(self, i: int) -> Canvas:
        return Canvas.from_nametable(self.nametables[i * 0x400:(i + 1) * 0x400])

    def scroll(self) -> tuple[int, int]:
        """The scroll origin in the 512 x 480 four-page plane, from the PPU's t register and fine X:
        what the next frame starts from."""
        t, fx = int(self.regs["ppu.tmpVideoRamAddr"]), int(self.regs["ppu.xScroll"])
        x = (t & 0x1F) * 8 + fx + ((t >> 10) & 1) * 256
        y = ((t >> 5) & 0x1F) * 8 + ((t >> 12) & 7) + ((t >> 11) & 1) * 240
        return x, y

    def frame(self, *, master=None, origin: tuple[int, int] | None = None,
              splits: list[tuple[int, tuple[int, int]]] | None = None) -> Image.Image:
        """The 256 x 240 picture from this state: background from one scroll origin (default:
        scroll()), then sprites by the hardware rules (left 8-pixel masks; per pixel the opaque
        sprite with the lowest OAM index wins, and only then does its behind-background bit
        apply, so a behind sprite also hides the sprites under it). A game that splits the screen
        mid-frame (a status bar) passes splits: [(first scanline, origin), ...] in order, each
        origin a scroll() pair, the frame's origin applying above the first. No
        8-sprites-per-line limit. For the exact picture use the emulator's own screenshot."""
        M = master or ppu.NES_MASTER
        plane = self.palette_indices()
        bands = [(0, origin or self.scroll())] + list(splits or [])
        img = Image.new("RGB", (256, 240))
        px = img.load()
        opaque = [[False] * 256 for _ in range(240)]
        show_bg, left_bg = self._flag("ppu.mask.backgroundEnabled"), self._flag("ppu.mask.backgroundMask")
        for y in range(240):
            ox, oy = next(o for first, o in reversed(bands) if first <= y)
            row = plane[(oy + y) % 480]
            for x in range(256):
                v = row[(ox + x) % 512] if show_bg and (left_bg or x >= 8) else 0
                px[x, y] = M[self.palette[v] & 0x3F]
                opaque[y][x] = v & 3 != 0
        if self._flag("ppu.mask.spritesEnabled"):
            self._sprites(px, opaque, M)
        return img

    def palette_indices(self) -> list[bytearray]:
        """The 512 x 480 four-page background as palette-RAM indices (0-15): sub-palette * 4 +
        pixel value, and 0 (the backdrop) wherever the pixel value is 0."""
        plane = [bytearray(512) for _ in range(480)]
        tiles = {}
        for i, (c0, r0) in enumerate([(0, 0), (32, 0), (0, 30), (32, 30)]):
            pg = self.page(i)
            for r in range(30):
                for c in range(32):
                    t, s = pg.tiles[r * 32 + c], pg.subpal[r * 32 + c] * 4
                    grid = tiles.get(t) or tiles.setdefault(t, ppu.decode_tile(self.chr, t, self.bg_chr_base))
                    for y in range(8):
                        dst, g = plane[(r0 + r) * 8 + y], grid[y]
                        for x in range(8):
                            dst[(c0 + c) * 8 + x] = s + g[x] if g[x] else 0
        return plane

    def _sprites(self, px, opaque, M):
        pal = list(self.palette[16:32])
        left = self._flag("ppu.mask.spriteMask")
        taken = [[False] * 256 for _ in range(240)]  # a sprite already decided this pixel
        for i in range(64):                          # OAM 0 first: frontmost
            y, tile, attr, x = self.oam[i * 4:i * 4 + 4]
            if y >= 0xEF:
                continue
            if self.sprite_h == 16:
                base, t0 = (tile & 1) * 0x1000, tile & 0xFE
                parts = [(t0 + 1, 0), (t0, 8)] if attr & 0x80 else [(t0, 0), (t0 + 1, 8)]
            else:
                base, parts = self.sprite_chr_base, [(tile, 0)]
            sub = pal[(attr & 3) * 4:(attr & 3) * 4 + 4]
            for t, dy in parts:
                grid = ppu.decode_tile(self.chr, t, base)
                for ty in range(8):
                    for tx in range(8):
                        c = grid[7 - ty if attr & 0x80 else ty][7 - tx if attr & 0x40 else tx]
                        X, Y = x + tx, y + 1 + dy + ty
                        if not c or X > 255 or Y > 239 or (X < 8 and not left) or taken[Y][X]:
                            continue
                        taken[Y][X] = True
                        if attr & 0x20 and opaque[Y][X]:
                            continue                 # behind the background (and hiding what is under it)
                        px[X, Y] = M[sub[c] & 0x3F]


def text(page: Canvas | bytes, charmap: dict[int, str], unknown: str = ".") -> list[str]:
    """A nametable page (Canvas or raw 1 KB) as 30 lines of text through a tile -> char map."""
    tiles = page.tiles if isinstance(page, Canvas) else list(page[:960])
    return ["".join(charmap.get(t, unknown) for t in tiles[r * 32:(r + 1) * 32]) for r in range(30)]
