"""machines.nes.ppu against two PPU rules (row O, 2026-10-23)."""
from PIL import Image

from emu_harness.machines.nes import ppu as NR

# One tile: row 0 all pixel value 1, everything else value 0.
TILE = bytes([0xFF] + [0] * 15)


def test_background_pixel_zero_is_the_universal_backdrop():
    """BG pixel value 0 shows $3F00 in every sub-palette; $3F04/$3F08/$3F0C are never displayed."""
    master = [(i, i, i) for i in range(64)]
    pal16 = [0x0F, 0x01, 0x02, 0x03, 0x20, 0x21, 0x22, 0x23] + [0] * 8
    img = NR.render_tiles(TILE, [0], pal16, cols=1, rows=1, subpalette=[1], master=master)
    assert img.getpixel((0, 0)) == master[0x21]          # value 1 -> sub-palette 1's entry 1
    assert img.getpixel((0, 1)) == master[0x0F]          # value 0 -> the backdrop, not $20


def test_sprite_top_line_is_oam_y_plus_one():
    """The PPU draws a sprite on the scanline after the Y its OAM entry names."""
    base = Image.new("RGB", (16, 16), (0, 0, 0))
    master = [(255, 255, 255)] * 64
    img = NR.render_sprites(TILE, bytes([4, 0, 0, 2]), [0, 0x30, 0, 0] * 4, base, master=master)
    assert img.getpixel((2, 4)) == (0, 0, 0)
    assert img.getpixel((2, 5)) == (255, 255, 255)
