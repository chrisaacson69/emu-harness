"""KOEI NES asset formats: WHERE a title's assets live and HOW they assemble; the pixels are
machines.nes.ppu's. Moved from koei-nes/assets (emu-harness MIGRATION.md row O)."""
from emu_harness.machines.nes import ppu as _NR

# The one RGB table every KOEI asset draws with (Chris, 2026-10-23: "use warm"). The 2C02 has no single
# true RGB; this is FCEUX's, the same 64 entries na1's render tools carry. Indices are what row O
# proved against Mesen; this only turns them into colours.
MASTER = _NR.NES_MASTER_FCEUX
