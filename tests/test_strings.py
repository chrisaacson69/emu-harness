"""passes/strings (MIGRATION.md row L): the judgments, on the real cases that set them."""
from emu_harness.passes.strings import (is_text, nul_runs, printable_runs, referenced_text,
                                        slug_anchor, slug_plain, slug_words)


def test_strict_filter_keeps_text_and_drops_tile_bytes():
    assert is_text(b"Hit any key")
    assert not is_text(b"MMMMMM")                 # one repeated byte: attribute data
    assert not is_text(b"%3d")                    # short / letterless (a referenced format string)
    # (CHR bytes such as b"bbXOB" pass this filter, as they did in pure_strings: CHR is kept out by
    # scanning PRG only.)


def test_referenced_text_allows_control_prefixes_but_not_table_bytes():
    assert referenced_text(b"\x1bC1An epidemic is\nspreading!")      # rot3k2: ESC colour code first
    assert referenced_text(b"You don't have enough gold\x1bW")        # GemFire: ESC after the text
    assert not referenced_text(bytes([1, 1, 0x0F, 0x0F, 1, 1, 1, 1, 0x11, 0x11]))   # bk tile bytes


def test_scans_and_slugs():
    assert nul_runs(b"\x00Spring\x00\x05Fall\x00") == [(1, b"Spring"), (9, b"Fall")]
    assert printable_runs(b"ab\x01Summer\x02", 3) == [(3, b"Summer")]
    assert slug_words("Break alliance with the %s family? ") == "break_alliance_with_the_family"
    assert slug_anchor(r"[1b]C1An epidemic is\nspreading!") == "c1an_epidemic_is_spreading"   # decode_str's \n
    assert slug_anchor("%3d") == "fmt__3d"        # rom-xref's own stem (in NA1's registry), kept verbatim
    assert slug_plain("  Hit any key ", 0x1234) == "hit_any_key" and slug_plain("!!", 0x1234) == "str_1234"
