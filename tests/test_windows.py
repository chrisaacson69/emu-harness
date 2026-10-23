"""Bank windows (machines/nes/memory.Windows) and the window evidence (window_votes).

The fold's oracle is the census in MIGRATION.md row N (every copy, every bank of 6 titles); these
pin the rules without a ROM."""
from emu_harness.machines.nes.memory import Cart, Windows, window_votes

MMC5 = Cart(5, 0x40000, True)          # 256K: 32 banks of 8K, 30/31 fixed at $C000/$E000
MMC1 = Cart(1, 0x40000, True)          # 256K: 16 banks of 16K, 15 fixed at $C000


def test_profile_lists_default_and_precedence():
    w = Windows.from_profile(MMC5, {"bank_base": 0xA000, "banks_at_8000": [1, 4],
                                    "banks_at_c000": [4, 30], "banks_at_e000": [31]})
    assert w.base(1) == 0x8000 and w.base(2) == 0xA000
    assert w.base(4) == 0x8000                       # in two lists: the first ($8000) wins
    assert Windows.from_profile(MMC1, {}).base(3) == 0x8000


def test_fixed_banks_come_from_the_cart():
    w = Windows.from_profile(MMC5, {"bank_base": 0xA000})
    assert w.fixed() == {30: 0xC000, 31: 0xE000}
    assert w.base(31) == 0xE000 and w.base(31, profile_only=True) == 0xA000
    assert Windows.from_profile(MMC1, {}).fixed() == {15: 0xC000}
    assert Windows.from_profile(Cart(0, 0x4000, False), {}).fixed() == {}


def test_cpu_prg_round_trip_and_fits():
    w = Windows.from_profile(MMC5, {"bank_base": 0xA000, "banks_at_8000": [12]})
    assert w.cpu(12 * 0x2000 + 0x56) == 0x8056
    assert w.cpu(13 * 0x2000 + 0x56) == 0xA056
    assert w.cpu(31 * 0x2000) == 0xE000
    assert w.prg(12, 0x8056) == w.prg(12, 0xA056) == 12 * 0x2000 + 0x56   # identity ignores the window
    assert w.fits(12, 0x8056) and not w.fits(12, 0xA056)
    assert w.fits(12, 0x9FFF) and not w.fits(12, 0xA000) and not w.fits(12, 0x7FFF)   # the edges
    assert Windows.from_profile(MMC1, {}).cpu(15 * 0x4000 + 0x10) == 0xC010


def test_window_votes_words_onto_entries():
    buf = bytearray(0x2000)
    buf[0x100:0x103] = bytes([0x20, 0x00, 0x80])     # JSR $8000 -> entry 0 if the bank is at $8000
    buf[0x200:0x202] = bytes([0x00, 0x80])           # a bare pointer word, same target
    buf[0x300:0x303] = bytes([0x20, 0x10, 0xA0])     # JSR $A010 -> not an entry at either base
    assert window_votes(bytes(buf), {0x0}) == {0x8000: 2, 0xA000: 0}
    assert window_votes(bytes(buf), {0x10}) == {0x8000: 0, 0xA000: 1}   # the JSR $A010
    assert window_votes(bytes(buf), {0x0}, after=frozenset({0x20})) == {0x8000: 1, 0xA000: 0}
