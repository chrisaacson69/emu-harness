"""passes/listing: the label pass over both listing dialects (MIGRATION.md row A3)."""
from emu_harness.passes import listing

REGISTRY = '''[ram]
"0x0073" = { name = "prg_bank_shadow" }
[prg.bank0]
"0xC54F" = { name = "nmi_off" }
"0x8010" = { name = "bank0_routine" }
[prg.bank15]
"0xC577" = { name = "set_prg_bank" }
'''


def test_disasm6_names_tokens_and_ram_but_not_immediates_or_the_offset_comment(tmp_path):
    reg = tmp_path / "labels.toml"
    reg.write_text(REGISTRY, encoding="utf-8")
    code, ram = listing.disasm6_maps(reg, fixed_bank=15)
    assert code[(15, 0xC54F)] == "nmi_off"            # $C000+ is the fixed bank's, whatever section
    assert code[(0, 0x8010)] == "bank0_routine"
    text = ("b15_c54f:   lda $73                   ; 3C54F:  a5 73\n"
            "            lda #$73                  ; 3C551:  a9 73\n"
            "            jsr b15_c577              ; 3C553:  20 77 c5\n"
            "            sta $2001                 ; 3C577:  8d 01 20")
    out, nc, nr, ins = listing.relabel_disasm6(text, code, ram, listing.fixed_anchors(code, 15))
    lines = out.splitlines()
    assert lines[0].startswith("nmi_off:") and "lda prg_bank_shadow" in lines[0]
    assert "lda #$73" in lines[1]                       # an immediate is never a reference
    assert "jsr set_prg_bank" in lines[2]
    assert lines[3].startswith("set_prg_bank:")         # anchor inserted from the PRG offset
    assert lines[3].endswith("; 3C577:  8d 01 20")      # the trailing comment is untouched
    assert (nc, nr, ins) == (2, 1, [(0x3C577, "set_prg_bank")])


def test_sweep_resolves_by_where_the_address_lives(tmp_path):
    reg = tmp_path / "labels.toml"
    reg.write_text(REGISTRY.replace("prg.bank15", "prg.bank7"), encoding="utf-8")
    ram, banks = listing.registry_maps(reg)
    src = "b00_8010:  jsr $8010   ; 00010: 20 10 80\n  lda $0073\n  jsr $c577\n"
    out, st = listing.relabel_sweep(src, 0, ram, banks, fixed_bank=7)
    assert out.startswith("bank0_routine:  jsr bank0_routine   ; 00010: 20 10 80")
    assert "lda prg_bank_shadow" in out and "jsr set_prg_bank" in out
    # in the fixed bank's own listing, a switchable-window address is left numeric (no own bank)
    out7, st7 = listing.relabel_sweep("  jsr $8010\n", 7, ram, banks, fixed_bank=7)
    assert "jsr $8010" in out7 and st7["numeric"] == 1
