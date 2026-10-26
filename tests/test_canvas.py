"""machines.nes.canvas: the virtual-canvas contract (row O4, 2026-10-26)."""
import pytest

from emu_harness.machines.nes import ppu
from emu_harness.machines.nes.canvas import Canvas


def test_from_nametable_applies_the_attribute_quadrants():
    page = bytearray(1024)
    page[960] = 0b11_10_01_00          # attr byte 0: TL=0 TR=1 BL=2 BR=3
    cv = Canvas.from_nametable(bytes(page))
    assert (cv.cols, cv.rows) == (32, 30)
    at = lambda c, r: cv.subpal[r * 32 + c]
    assert [at(0, 0), at(2, 0), at(0, 2), at(3, 3)] == [0, 1, 2, 3]


def test_paste_and_crop_round_trip():
    world = Canvas.blank(8, 6)
    piece = Canvas(2, 2, [1, 2, 3, 4], [0, 1, 2, 3])
    world.paste(piece, 5, 3)
    assert world.crop(5, 3, 2, 2) == piece
    assert world.tiles[3 * 8 + 4] == 0
    with pytest.raises(ValueError):
        world.paste(piece, 7, 0)


def test_render_uses_per_tile_subpalettes():
    chr = bytes([0xFF] * 8 + [0] * 8)                   # tile 0: every pixel value 1
    master = [(i, i, i) for i in range(64)]
    pal = [0x0F, 0x01, 0, 0, 0x0F, 0x21, 0, 0] + [0] * 8
    img = Canvas(2, 1, [0, 0], [0, 1]).render(chr, pal, master=master)
    assert img.size == (16, 8)
    assert img.getpixel((0, 0)) == master[0x01] and img.getpixel((8, 0)) == master[0x21]
