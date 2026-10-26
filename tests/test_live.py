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
