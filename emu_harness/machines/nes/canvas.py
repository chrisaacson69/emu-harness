"""The virtual canvas: a nametable of any size, for a game's map decoder to target (row O4).

A game decodes its own map format (Zelda's column heaps, KOEI's fief tables) into a `Canvas`: a
cols x rows grid of tile indices plus one sub-palette id (0-3) per tile. The grid is not bound to
the PPU's 32x30 page, so a whole world is one canvas. CHR and the 16-entry palette are given at
render time, because many screens share them; `render` draws through `ppu.render_tiles`, so every
PPU rule (backdrop, 2bpp decode, master table) is the library's, never the game's.

Per-tile sub-palettes rather than an attribute table, so canvases paste at any tile offset. A real
PPU page converts with `from_nametable`, which applies the hardware 2x2-quadrant attribute rule.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import ppu


@dataclass
class Canvas:
    cols: int
    rows: int
    tiles: list[int]
    subpal: list[int]

    def __post_init__(self):
        n = self.cols * self.rows
        if len(self.tiles) != n or len(self.subpal) != n:
            raise ValueError(f"canvas {self.cols}x{self.rows} needs {n} tiles and sub-palettes")

    @classmethod
    def blank(cls, cols: int, rows: int, tile: int = 0, sub: int = 0) -> Canvas:
        return cls(cols, rows, [tile] * (cols * rows), [sub] * (cols * rows))

    @classmethod
    def from_nametable(cls, page: bytes) -> Canvas:
        """One 1 KB PPU nametable page (960 tile bytes + 64 attribute bytes) -> a 32x30 canvas."""
        attr = page[960:1024]
        return cls(32, 30, list(page[:960]),
                   [ppu.attr_subpalette(attr, c, r) for r in range(30) for c in range(32)])

    def paste(self, other: Canvas, col: int, row: int) -> None:
        """Copy `other` into this canvas with its top-left tile at (col, row)."""
        if col < 0 or row < 0 or col + other.cols > self.cols or row + other.rows > self.rows:
            raise ValueError(f"{other.cols}x{other.rows} at ({col},{row}) leaves {self.cols}x{self.rows}")
        for r in range(other.rows):
            d, s = (row + r) * self.cols + col, r * other.cols
            self.tiles[d:d + other.cols] = other.tiles[s:s + other.cols]
            self.subpal[d:d + other.cols] = other.subpal[s:s + other.cols]

    def crop(self, col: int, row: int, cols: int, rows: int) -> Canvas:
        out = Canvas.blank(cols, rows)
        for r in range(rows):
            s, d = (row + r) * self.cols + col, r * cols
            out.tiles[d:d + cols] = self.tiles[s:s + cols]
            out.subpal[d:d + cols] = self.subpal[s:s + cols]
        return out

    def render(self, chr: bytes, palette16, *, chr_base: int = 0, scale: int = 1, master=None):
        """-> PIL.Image, cols*8 x rows*8 (times scale)."""
        return ppu.render_tiles(chr, self.tiles, palette16, cols=self.cols, rows=self.rows,
                                subpalette=self.subpal, chr_base=chr_base, scale=scale, master=master)
