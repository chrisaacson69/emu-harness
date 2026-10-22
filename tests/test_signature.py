"""Row E: the 6502 code signature, the fixed bank, the by-address names view."""
from emu_harness.core.symbols import Registry, Entry, names_by_address
from emu_harness.cpu.m6502.signature import best_match, signature
from emu_harness.machines.nes.memory import fixed_bank

# lda #$05 / sta $0050 / jsr $C123 / bne -4 / rts
CODE = bytes([0xA9, 0x05, 0x8D, 0x50, 0x00, 0x20, 0x23, 0xC1, 0xD0, 0xFC, 0x60])


def test_signature_keeps_ram_and_immediates_normalizes_rom_and_branches():
    assert signature(CODE, 0xC000) == ["lda#05", "sta/6/0050", "jsr/6/ROM", "bne", "rts"]


def test_signature_is_relocation_invariant():
    moved = bytes([0xA9, 0x05, 0x8D, 0x50, 0x00, 0x20, 0x99, 0xE7, 0xD0, 0x10, 0x60])
    assert signature(moved, 0xC000) == signature(CODE, 0xC000)
    other = bytes([0xA9, 0x05, 0x8D, 0x51, 0x00, 0x60])             # different RAM operand
    assert best_match(signature(CODE, 0xC000), {0xD000: signature(other, 0xC000),
                                                0xE000: signature(moved, 0xC000)}) == (0xE000, 1.0)


def test_rom_boundary_is_8000():
    # jsr $8123 (switchable window, relocates) vs lda $7FFF (cartridge RAM, kept)
    assert signature(bytes([0x20, 0x23, 0x81, 0xAD, 0xFF, 0x7F, 0x60]), 0xC000) == \
        ["jsr/6/ROM", "lda/6/7fff", "rts"]


def test_non_opcodes_are_data_tokens():
    assert signature(bytes([0x02, 0x60]), 0xC000) == [".db", "rts"]


def test_rti_ends_a_routine():                     # interrupt handlers (NMI) end in rti
    assert signature(bytes([0xEA, 0x40, 0xEA]), 0xC000) == ["nop", "rti"]


def test_fixed_bank_is_the_last_16k_of_prg():
    def rom(banks):
        return b"NES\x1a" + bytes([banks]) + bytes(11) + b"".join(bytes([k]) * 0x4000 for k in range(banks))
    assert fixed_bank(rom(16))[0] == 15          # 256K: bank 15, as handler_match.bank15 assumed
    assert fixed_bank(rom(32))[0] == 31          # 512K (UW): NOT bank 15
    assert fixed_bank(rom(8))[0] == 7            # 128K (Elite)


def test_names_by_address_first_entry_wins():
    reg = Registry(path=None, entries=[Entry("prg.bank0", 0xC7C9, "a", "", 1), Entry("prg.bank15", 0xC7C9, "b", "", 9)])
    assert names_by_address(reg) == {0xC7C9: "a"}
