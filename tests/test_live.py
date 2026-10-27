"""machines.nes.live without an emulator: input scripts, nametable text, the scroll origin."""
from emu_harness.machines.nes.canvas import Canvas
from emu_harness.machines.nes.live import PPUSnapshot, play, text


class Recorder:
    def __init__(self):
        self.calls = []

    def step(self, buttons=(), frames=1):
        self.calls.append((tuple(buttons), frames))


def test_play_script_tokens():
    m = Recorder()
    toks = list(play(m, "wait*10, a*2, hold:left+b*30", press=3, gap=5))
    assert toks == ["wait*10", "a*2", "hold:left+b*30"]
    assert m.calls == [((), 10), (("a",), 3), ((), 5), (("a",), 3), ((), 5), (("left", "b"), 30)]


def test_text_reads_a_page_through_a_charmap():
    page = bytearray(1024)
    page[33:36] = b"\x0a\x0b\x0c"
    lines = text(bytes(page), {0x0A: "A", 0x0B: "B", 0x0C: "C", 0: " "})
    assert len(lines) == 30 and lines[1] == " ABC" + " " * 28
    assert text(Canvas.from_nametable(bytes(page)), {0: " "}, "?")[1][1:4] == "???"


def test_scroll_origin_from_t_and_fine_x():
    regs = {"ppu.tmpVideoRamAddr": str(0b011_10_00010_00011), "ppu.xScroll": "5"}
    snap = PPUSnapshot(b"", b"", b"", b"", regs)
    # coarse X 3, coarse Y 2, nametable 2 (bottom-left), fine Y 3
    assert snap.scroll() == (3 * 8 + 5, 2 * 8 + 3 + 240)


def _snap(page0_tile, page1_tile, sprites=()):
    """A 2-tile CHR (tile 1 solid colour 1), two pages of one tile each, palette 0 = $0F backdrop,
    $01 the background colour, sprite palettes 0/1 = $21 / $22; OAM unused slots off screen."""
    chr_ = bytearray(0x2000)
    chr_[16:24] = b"\xff" * 8
    nt = bytearray(0x1000)
    nt[0:960], nt[0x400:0x400 + 960] = bytes([page0_tile]) * 960, bytes([page1_tile]) * 960
    pal = bytearray(32)
    pal[0], pal[1], pal[17], pal[21] = 0x0F, 0x01, 0x21, 0x22
    oam = bytearray(b"\xff" * 256)
    for i, (y, attr, x) in enumerate(sprites):
        oam[4 * i:4 * i + 4] = bytes([y, 1, attr, x])
    regs = {"ppu.tmpVideoRamAddr": "0", "ppu.xScroll": "0", "ppu.control.backgroundPatternAddr": "0",
            "ppu.control.spritePatternAddr": "0", "ppu.control.largeSprites": "false",
            "ppu.mask.backgroundEnabled": "true", "ppu.mask.backgroundMask": "true",
            "ppu.mask.spritesEnabled": "true", "ppu.mask.spriteMask": "true"}
    return PPUSnapshot(bytes(chr_), bytes(nt), bytes(pal), bytes(oam), regs)


def test_a_behind_sprite_hides_the_sprites_under_it():
    from emu_harness.machines.nes import ppu
    M = ppu.NES_MASTER
    # OAM 0 behind the background, OAM 1 in front, both at (16, 17): over an opaque background
    # the pixel is the background's (OAM 0 decided it); over the backdrop it is OAM 0's colour
    img = _snap(1, 0, [(16, 0x20, 16), (16, 0x01, 16)]).frame().load()
    assert img[20, 20] == M[0x01]
    img = _snap(0, 0, [(16, 0x20, 16), (16, 0x01, 16)]).frame().load()
    assert img[20, 20] == M[0x21]


def test_splits_change_the_scroll_origin_from_a_scanline():
    from emu_harness.machines.nes import ppu
    M = ppu.NES_MASTER
    img = _snap(1, 0).frame(splits=[(8, (256, 0))]).load()     # rows 0-7 page 0, then page 1
    assert img[100, 7] == M[0x01] and img[100, 8] == M[0x0F]
