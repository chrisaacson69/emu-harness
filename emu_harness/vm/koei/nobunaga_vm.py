"""
Nobunaga VM runner.

Wraps cpu6502.Memory + CPU6502 with:
  - SRAM dump loading
  - MMC1 bank switching
  - mesen-labels.toml integration for symbolic output
  - Host-call stubs for native helper subs (rng_mod, abs, math32_3arg)
  - VM trace mode that emits readable per-opcode log lines

The VM runs by jumping CPU to $E867 (dispatcher entry) and stepping until
a stop condition is met. Each opcode trip through the dispatcher returns
CPU PC to $E867 — that's the natural per-opcode boundary.
"""

import sys
import re
from pathlib import Path

from emu_harness.vm.koei.cpu6502 import Memory, CPU6502

# Data anchors: ROM + labels live at the project root (nobunaga/), the opcode table ships
# inside the package. _PKG = koeivm/, _ROOT = nobunaga/ (package -> tools -> nobunaga).
_PKG = Path(__file__).parent
_ROOT = _PKG.parent.parent
ROM_PATH = _ROOT / "Nobunaga's Ambition (USA).nes"
LABELS_PATH = _ROOT / "mesen-labels.toml"
OPCODES_PATH = _PKG / "vm-opcodes-v2.toml"


# ============================================================================
# Label loading
# ============================================================================

def load_labels():
    """Parse mesen-labels.toml without needing the tomllib dep (regex is fine).

    Returns: dict address (int) -> (name, comment).
    """
    # koeivm is title-agnostic: the NA1-layout default LABELS_PATH may not exist (e.g. when
    # driven by koei_vm for another title, which supplies labels separately). Degrade to empty.
    if not LABELS_PATH.exists():
        return {}
    text = LABELS_PATH.read_text(encoding="utf-8")
    labels = {}
    # Match lines like: "0x87F0" = { name = "effect_grow", comment = "..." }
    line_re = re.compile(
        r'"0x([0-9A-Fa-f]+)"\s*=\s*\{\s*name\s*=\s*"([^"]+)"(?:,\s*comment\s*=\s*"([^"]*)")?'
    )
    for m in line_re.finditer(text):
        addr = int(m.group(1), 16)
        name = m.group(2)
        comment = m.group(3) or ""
        labels[addr] = (name, comment)
    return labels


def load_labels_by_prg():
    """Section-aware label load keyed by PRG ROM offset (0-0x3FFFF) — the bank-
    unambiguous identity. The flat load_labels() collides banks at the same CPU
    address ($8000-$BFFF is a switchable window: bank0 $BA10 and bank1 $BA10 are
    different bytes but one key), so analysis tools that must disambiguate banks
    use THIS. Returns prg_off -> (name, comment, bank, cpu_addr). ROM only
    ($8000-$FFFF); [ram]/SRAM labels have no PRG offset and are skipped.

    PRG offset = bank*0x4000 + (cpu & 0x3FFF), matching mesen-labels.py's .mlb
    keying. $C000-$FFFF is the MMC1 FIXED bank 15 (address-authoritative, so the
    ~60 kernel labels mis-filed under [prg.bank0] still resolve correctly)."""
    text = LABELS_PATH.read_text(encoding="utf-8")
    sec_re = re.compile(r'^\s*\[prg\.bank(\d+)\]')
    lbl_re = re.compile(
        r'"0x([0-9A-Fa-f]+)"\s*=\s*\{\s*name\s*=\s*"([^"]+)"(?:,\s*comment\s*=\s*"([^"]*)")?')
    out, cur_bank = {}, None
    for line in text.splitlines():
        sm = sec_re.match(line)
        if sm:
            cur_bank = int(sm.group(1))
            continue
        if line.lstrip().startswith('['):       # left the prg.bankN sections
            cur_bank = None
            continue
        m = lbl_re.search(line)
        if not m:
            continue
        cpu = int(m.group(1), 16)
        if cpu >= 0xC000:
            bank = 15                            # fixed bank, address wins over section
        elif 0x8000 <= cpu < 0xC000:
            bank = cur_bank                      # switchable: the section IS the bank
            if bank is None:
                continue                         # un-sectioned window label: ambiguous
        else:
            continue                             # RAM/SRAM: no PRG identity
        out[bank * 0x4000 + (cpu & 0x3FFF)] = (m.group(2), m.group(3) or "", bank, cpu)
    return out


# ============================================================================
# Opcode table — minimal version for trace formatting
# ============================================================================
# Maps the leading opcode byte to (mnemonic, operand-bytes).
# Negative operand_bytes = take from low nibble of opcode.
OPCODE_INFO = {}

def _init_opcode_info():
    # Quick groups (operand encoded in low nibble)
    for op in range(0x00, 0x10): OPCODE_INFO[op] = ("LOADL_quick", 0)
    for op in range(0x10, 0x20): OPCODE_INFO[op] = ("LOADR_quick", 0)
    for op in range(0x20, 0x30): OPCODE_INFO[op] = ("STORE_quick", 0)
    for op in range(0x30, 0x40): OPCODE_INFO[op] = ("PUSH_quick", 0)
    for op in range(0x40, 0x50): OPCODE_INFO[op] = ("LOADL_qimm", 0)
    for op in range(0x50, 0x60): OPCODE_INFO[op] = ("LOADR_qimm", 0)
    for op in range(0x60, 0x70): OPCODE_INFO[op] = ("PUSH_qimm", 0)
    for op in range(0x70, 0x80): OPCODE_INFO[op] = ("ADD_qimm", 0)
    OPCODE_INFO[0x80] = ("ILLEGAL", 0)
    OPCODE_INFO[0x81] = ("LOADL_near", 1)
    OPCODE_INFO[0x82] = ("LOADL_far", 2)
    OPCODE_INFO[0x83] = ("LOADR_near", 1)
    OPCODE_INFO[0x84] = ("LOADR_far", 2)
    OPCODE_INFO[0x85] = ("STORE_near", 1)
    OPCODE_INFO[0x86] = ("STORE_far", 2)
    OPCODE_INFO[0x87] = ("PUSH_near", 1)
    OPCODE_INFO[0x88] = ("PUSH_far", 2)
    OPCODE_INFO[0x89] = ("BYTE_LOADL_imm1", 1)
    OPCODE_INFO[0x8A] = ("LOADL_imm2", 2)
    OPCODE_INFO[0x8B] = ("BYTE_LOADR_imm1", 1)
    OPCODE_INFO[0x8C] = ("LOADR_imm2", 2)
    OPCODE_INFO[0x8D] = ("BYTE_PUSH_imm1", 1)
    OPCODE_INFO[0x8E] = ("PUSH_imm2", 2)
    OPCODE_INFO[0x8F] = ("BYTE_ADD_imm1", 1)
    OPCODE_INFO[0x90] = ("ADD_imm2", 2)
    OPCODE_INFO[0xA0] = ("BYTE_LOADL_far", 2)
    OPCODE_INFO[0xA1] = ("BYTE_LOADR_far", 2)
    OPCODE_INFO[0xA2] = ("BYTE_STORE_far", 2)
    OPCODE_INFO[0xA3] = ("BYTE_PUSH_far", 2)
    OPCODE_INFO[0xA4] = ("LOADL_abs", 2)
    OPCODE_INFO[0xA5] = ("BYTE_LOADL_abs", 2)
    OPCODE_INFO[0xA6] = ("LOADR_abs", 2)
    OPCODE_INFO[0xA7] = ("BYTE_LOADR_abs", 2)
    OPCODE_INFO[0xA8] = ("STORE_abs", 2)
    OPCODE_INFO[0xA9] = ("BYTE_STORE_abs", 2)
    OPCODE_INFO[0xAA] = ("PUSH_abs", 2)
    OPCODE_INFO[0xAB] = ("BYTE_PUSH_abs", 2)
    OPCODE_INFO[0xAC] = ("CALL_abs", 2)
    OPCODE_INFO[0xAD] = ("COPY_imm2", 4)
    OPCODE_INFO[0xAE] = ("UNSTACK_imm1", 1)
    OPCODE_INFO[0xAF] = ("UNSTACK_imm2", 2)
    OPCODE_INFO[0xB0] = ("DEREF", 0)
    OPCODE_INFO[0xB1] = ("POPSTORE", 0)
    OPCODE_INFO[0xB2] = ("NOP", 0)
    OPCODE_INFO[0xB3] = ("PUSHL", 0)
    OPCODE_INFO[0xB4] = ("POPR", 0)
    OPCODE_INFO[0xB5] = ("MULT", 0)
    OPCODE_INFO[0xB6] = ("SDIV", 0)
    OPCODE_INFO[0xB7] = ("LONG", 1)  # next byte selects 32-bit op
    OPCODE_INFO[0xB8] = ("UDIV", 0)
    OPCODE_INFO[0xB9] = ("SMOD", 0)
    OPCODE_INFO[0xBA] = ("UMOD", 0)
    OPCODE_INFO[0xBB] = ("ADD", 0)
    OPCODE_INFO[0xBC] = ("SUB", 0)
    OPCODE_INFO[0xBD] = ("LSHIFT", 0)
    OPCODE_INFO[0xBE] = ("URSHIFT", 0)
    OPCODE_INFO[0xBF] = ("SRSHIFT", 0)
    OPCODE_INFO[0xC0] = ("CMPEQ", 0)
    OPCODE_INFO[0xC1] = ("CMPNE", 0)
    OPCODE_INFO[0xC2] = ("SCMPLT", 0)
    OPCODE_INFO[0xC3] = ("SCMPLE", 0)
    OPCODE_INFO[0xC4] = ("SCMPGT", 0)
    OPCODE_INFO[0xC5] = ("SCMPGE", 0)
    OPCODE_INFO[0xC6] = ("UCMPLT", 0)
    OPCODE_INFO[0xC7] = ("UCMPLE", 0)
    OPCODE_INFO[0xC8] = ("UCMPGT", 0)
    OPCODE_INFO[0xC9] = ("UCMPGE", 0)
    OPCODE_INFO[0xCA] = ("NOT", 0)
    OPCODE_INFO[0xCB] = ("MINUS", 0)
    OPCODE_INFO[0xCC] = ("COMPL", 0)
    OPCODE_INFO[0xCD] = ("SWAP", 0)
    OPCODE_INFO[0xCE] = ("ILLEGAL", 0)
    OPCODE_INFO[0xCF] = ("RETURN", 0)
    OPCODE_INFO[0xD0] = ("INC", 0)
    OPCODE_INFO[0xD1] = ("DEC", 0)
    OPCODE_INFO[0xD2] = ("LSHIFT1", 0)
    OPCODE_INFO[0xD3] = ("BYTE_DEREF", 0)
    OPCODE_INFO[0xD4] = ("BYTE_POPSTORE", 0)
    OPCODE_INFO[0xD5] = ("SWITCH_contig", 6)  # offs(2) + num(2) + default(2)
    OPCODE_INFO[0xD6] = ("JUMP_abs", 2)
    OPCODE_INFO[0xD7] = ("JUMPT_abs", 2)
    OPCODE_INFO[0xD8] = ("JUMPF_abs", 2)
    OPCODE_INFO[0xD9] = ("SWITCH_noncontig", -1)  # variable length
    OPCODE_INFO[0xDA] = ("AND", 0)
    OPCODE_INFO[0xDB] = ("OR", 0)
    OPCODE_INFO[0xDC] = ("XOR", 0)
    OPCODE_INFO[0xDD] = ("CALLPTR", 0)
    OPCODE_INFO[0xDE] = ("LEAL_far", 2)
    OPCODE_INFO[0xDF] = ("LEAR_far", 2)
    OPCODE_INFO[0xE0] = ("SLOADBF", 1)
    OPCODE_INFO[0xE1] = ("ULOADBF", 1)
    OPCODE_INFO[0xE2] = ("STOREBF", 1)
    OPCODE_INFO[0xE3] = ("JUMP_back", 1)
    OPCODE_INFO[0xE4] = ("JUMPT_back", 1)
    OPCODE_INFO[0xE5] = ("JUMPF_back", 1)
    OPCODE_INFO[0xE6] = ("JUMP_ahead", 1)
    OPCODE_INFO[0xE7] = ("JUMPT_ahead", 1)
    OPCODE_INFO[0xE8] = ("JUMPF_ahead", 1)
    OPCODE_INFO[0xE9] = ("CALL_abs_imm1", 3)  # word + adj_byte
    OPCODE_INFO[0xEA] = ("CALLPTR_imm1", 1)

_init_opcode_info()


# ============================================================================
# VM Runner
# ============================================================================

class NobunagaVM:
    DISPATCHER_ADDR = 0xE867

    # VM opcodes that perform a VM-level CALL (increase call depth by 1)
    VM_CALL_OPCODES = {0xAC, 0xDD, 0xE9, 0xEA}
    # VM opcode that performs a VM-level RETURN (decrease call depth by 1)
    VM_RETURN_OPCODE = 0xCF

    def __init__(self, rom_path=None, labels_path=None):
        rom_path = rom_path or ROM_PATH
        self.rom = Path(rom_path).read_bytes()
        self.mem = Memory(self.rom)
        self.cpu = CPU6502(self.mem)
        self.labels = load_labels()
        self.trace_enabled = False
        self.trace_log = []
        self.vm_call_depth = 0   # nested VM call depth — used for outermost-return detection
        self.host_call_stubs = {}  # VM addr (target of CALL_abs) -> python callable(vm) -> regL value
        self.syscall_log = []      # (task, name, kind) for each $F226 dispatch — observability
        self._rng = 0x1234         # deterministic RNG state for syscall_rng_next
        self.install_stub(0xF226, self._syscall_dispatch)  # kernel syscall dispatch ($F226)

        # Initialize VM zero-page state
        self.mem.write(0x02, 0xFF); self.mem.write(0x03, 0x05)  # VM SP = $05FF
        self.mem.write(0x04, 0xFF); self.mem.write(0x05, 0x05)  # VM FP = $05FF
        self.mem.write(0x06, 0x00); self.mem.write(0x07, 0x00)  # VM PC = $0000

    def load_sram(self, dmp_path):
        """Load an 8 KB SRAM dump into $6000-$7FFF."""
        data = Path(dmp_path).read_bytes()
        assert len(data) == 0x2000, f"SRAM dump should be 8KB, got {len(data)}"
        self.mem.sram[:] = data

    def switch_bank(self, bank):
        self.mem.switch_bank(bank)

    def label(self, addr):
        """Look up a label for an address. Returns the name or None."""
        if addr in self.labels:
            return self.labels[addr][0]
        return None

    def fmt_addr(self, addr):
        """Format an address with its label, if any."""
        lbl = self.label(addr)
        if lbl: return f"${addr:04X} ({lbl})"
        return f"${addr:04X}"

    # ---- VM state accessors ---------------------------------------------
    @property
    def vm_pc(self): return self.mem.read_word(0x06)
    @vm_pc.setter
    def vm_pc(self, v): self.mem.write_word(0x06, v)

    @property
    def vm_sp(self): return self.mem.read_word(0x02)
    @vm_sp.setter
    def vm_sp(self, v): self.mem.write_word(0x02, v)

    @property
    def vm_fp(self): return self.mem.read_word(0x04)
    @vm_fp.setter
    def vm_fp(self, v): self.mem.write_word(0x04, v)

    @property
    def regL(self): return self.mem.read_word(0x08)
    @regL.setter
    def regL(self, v): self.mem.write_word(0x08, v)

    @property
    def regR(self): return self.mem.read_word(0x0C)
    @regR.setter
    def regR(self, v): self.mem.write_word(0x0C, v)

    def vm_push(self, val):
        """Push a 16-bit word onto the VM data stack (descending)."""
        sp = self.vm_sp
        # Store LSB at sp, MSB at sp-1, then sp -= 2 (descending full)
        self.mem.write(sp, val & 0xFF)
        self.mem.write((sp - 1) & 0xFFFF, (val >> 8) & 0xFF)
        self.vm_sp = (sp - 2) & 0xFFFF

    # ---- Tracing ---------------------------------------------------------
    def fmt_vm_state(self):
        return f"L=${self.regL:04X} R=${self.regR:04X} SP=${self.vm_sp:04X} FP=${self.vm_fp:04X}"

    def disasm_at(self, pc):
        """Disassemble one VM opcode at pc. Returns (mnemonic_string, instr_length)."""
        op = self.mem.read(pc)
        info = OPCODE_INFO.get(op, ("UNK", 0))
        mnemonic, n_operand = info
        # Fast-group: show low nibble as operand
        if op < 0x80:
            return f"{mnemonic} #{op & 0x0F}", 1
        if n_operand == 0:
            return mnemonic, 1
        if n_operand == 1:
            b = self.mem.read(pc + 1)
            return f"{mnemonic} ${b:02X}", 2
        if n_operand == 2:
            w = self.mem.read(pc + 1) | (self.mem.read(pc + 2) << 8)
            lbl = self.label(w)
            if lbl: return f"{mnemonic} ${w:04X} ({lbl})", 3
            return f"{mnemonic} ${w:04X}", 3
        if n_operand == 3:  # word + byte (CALL_abs_imm1)
            w = self.mem.read(pc + 1) | (self.mem.read(pc + 2) << 8)
            b = self.mem.read(pc + 3)
            lbl = self.label(w)
            tgt = f"${w:04X} ({lbl})" if lbl else f"${w:04X}"
            return f"{mnemonic} {tgt}, #${b:02X}", 4
        # Variable length
        return f"{mnemonic} ?", 1

    # ---- Step / run ------------------------------------------------------
    def step_one_vm_op(self, max_cpu_steps=2000):
        """Step the CPU until it completes ONE VM opcode (i.e., lands back
        at the dispatcher entry $E867).

        Tracks CALL/RETURN to maintain vm_call_depth.

        Returns: opcode byte that was executed, or None on timeout.
        """
        if self.cpu.pc != self.DISPATCHER_ADDR:
            for _ in range(max_cpu_steps):
                if self.cpu.pc == self.DISPATCHER_ADDR:
                    break
                self.cpu.step()
            else:
                return None

        # Capture pre-state and read upcoming opcode
        pre_pc = self.vm_pc
        upcoming_op = self.mem.read(pre_pc)

        # Check for host_call stub — intercept BEFORE entering the call
        # (for CALL_abs_imm1 = $E9, the target is at PC+1 as a word operand)
        if upcoming_op == 0xE9:
            call_target = self.mem.read(pre_pc + 1) | (self.mem.read(pre_pc + 2) << 8)
            if call_target in self.host_call_stubs:
                # Skip the CALL entirely — run the Python stub, set regL,
                # and advance VM PC past the 4-byte CALL_abs_imm1 instruction.
                stub_result = self.host_call_stubs[call_target](self)
                if stub_result is not None:
                    self.regL = stub_result & 0xFFFF
                # The CALL_abs_imm1 instruction is 4 bytes: opcode + word + adj_byte
                # The adj byte adjusts ptr1 (data stack) AFTER call; since we're
                # short-circuiting, we apply the adjustment to VM SP directly.
                adj = self.mem.read(pre_pc + 3)
                self.vm_sp = (self.vm_sp + adj) & 0xFFFF
                self.vm_pc = (pre_pc + 4) & 0xFFFF
                if self.trace_enabled:
                    name = self.label(call_target) or f"${call_target:04X}"
                    self.trace_log.append(
                        f"  ${pre_pc:04X}  STUB CALL {name}, sp_adj=#${adj:02X}  -> "
                        f"regL=${self.regL:04X}"
                    )
                return upcoming_op

        # Disasm for trace
        if self.trace_enabled:
            disasm, _ = self.disasm_at(pre_pc)

        # Track VM call depth
        if upcoming_op in self.VM_CALL_OPCODES:
            self.vm_call_depth += 1
        elif upcoming_op == self.VM_RETURN_OPCODE:
            self.vm_call_depth -= 1

        # Step the CPU past $E867 entry, then run until next dispatcher boundary
        self.cpu.step()
        for _ in range(max_cpu_steps):
            self.cpu.step()
            if self.cpu.pc == self.DISPATCHER_ADDR:
                if self.trace_enabled:
                    state = self.fmt_vm_state()
                    depth_marker = "  " * max(0, self.vm_call_depth)
                    self.trace_log.append(
                        f"  ${pre_pc:04X}  {depth_marker}{disasm:<38}  -> {state}"
                    )
                return upcoming_op
        return None

    def run_until_outermost_return(self, max_ops=20000):
        """Run VM opcodes until just BEFORE the outermost RETURN executes.

        We stop BEFORE running the outermost CF because executing it would
        restore garbage frame state (we have no real caller) and crash.
        regL at that point holds the sub's return value.

        Returns True if reached, False on timeout.
        """
        self.vm_call_depth = 0
        for _ in range(max_ops):
            # Peek at upcoming opcode
            if self.cpu.pc == self.DISPATCHER_ADDR:
                upcoming = self.mem.read(self.vm_pc)
                if upcoming == self.VM_RETURN_OPCODE and self.vm_call_depth == 0:
                    # About to execute outermost RETURN — stop here
                    if self.trace_enabled:
                        self.trace_log.append(
                            f"  ${self.vm_pc:04X}  (outermost RETURN — stopping)  -> regL=${self.regL:04X}"
                        )
                    return True

            op = self.step_one_vm_op()
            if op is None:
                return False
        return False

    def run_n_ops(self, n):
        """Run N VM opcodes."""
        for _ in range(n):
            if self.step_one_vm_op() is None:
                return False
        return True

    def install_stub(self, target_addr, fn):
        """Install a Python stub for CALL_abs_imm1 ($E9) to `target_addr`.
        fn(vm) is called instead of executing the sub; if it returns an
        integer, that becomes regL.
        """
        self.host_call_stubs[target_addr] = fn

    # ===================== Kernel syscall layer ($F226) =====================
    # The 23-entry kernel syscall table at $C173, dispatched by syscall_dispatch
    # ($F226). Headless we can't run the native handlers (PPU/NMI-wait spin/MMC1
    # serial writes), so we resolve syscalls in Python. Calling convention
    # (chapter-4 labels, verified against the map-render trace 2026-05-30):
    #   - task id is pushed via op_8D just before `host_call $F226`, so it lands
    #     at the TOP of the VM data stack -> struct[0] = mem[vm_sp].
    #   - remaining params are struct[n] = mem[vm_sp + n] (words, LE).
    #   - returns go in brk_scratch_lo/hi ($66/$67).
    #   - the host_call adj byte (struct size) is popped by the $E9 stub plumbing.
    # Bank switching is emulated by READING the target ROM page directly (per the
    # "it's just pointing at a different page of memory" insight) — no MMC1 dance.
    #
    # kind: EMU = emulated (RAM/compute, reproducible) · STUB = no-op (PPU/audio/
    # input/hardware — irrelevant to a headless RAM model) · TODO = not yet worked
    # out (auto-stub + logged; the unnamed handlers + call_bank/sram are deferred
    # until a use case forces them — see ROADMAP).
    SYSCALLS = {
        0x00: ("syscall_00",         "TODO"), 0x01: ("ppu_upload_block",   "STUB"),
        0x02: ("syscall_02",         "TODO"), 0x03: ("set_sprite",         "EMU"),
        0x04: ("palette_write",      "EMU"),  0x05: ("fill_nametable",     "STUB"),
        0x06: ("read_controller",    "STUB"), 0x07: ("call_bank",          "EMU"),
        0x08: ("fill_attr_quadrant", "STUB"), 0x09: ("audio_load_voice",   "EMU"),
        0x0A: ("audio_control",      "STUB"), 0x0B: ("syscall_0B",         "TODO"),
        0x0C: ("ppu_fill_rect",    "STUB"), 0x0D: ("ppu_render_rect",    "STUB"),
        0x0E: ("syscall_0E",         "TODO"), 0x0F: ("syscall_0F",         "TODO"),
        0x10: ("memcpy_banked",      "EMU"),  0x11: ("rng_next",           "EMU"),
        0x12: ("palette_swap",       "EMU"),  0x13: ("wait_for_nmi",       "STUB"),
        0x14: ("ppu_copy_rect", "STUB"), 0x15: ("set_chr_bank0_reg",  "STUB"),
        0x16: ("sram_block_checksum","TODO"),
    }

    def read_banked(self, bank, addr):
        """Read one byte as if `bank` were mapped into $8000-$BFFF. $C000+ is the
        fixed bank 15; below $8000 is live RAM/SRAM (bank-agnostic)."""
        if addr >= 0xC000:
            return self.rom[0x10 + 15 * 0x4000 + (addr - 0xC000)]
        if addr >= 0x8000:
            return self.rom[0x10 + (bank & 0x0F) * 0x4000 + (addr - 0x8000)]
        return self.mem.read(addr)

    def _syscall_dispatch(self, vm):
        sp = self.vm_sp
        rd = self.mem.read
        rw = self.mem.read_word
        task = rd(sp)
        name, kind = self.SYSCALLS.get(task, (f"syscall_{task:02X}", "TODO"))

        if task == 0x10:        # memcpy_banked: struct = [task][bank][src][dst][len]
            # at sp+0/+2/+4/+6/+8 (one word each; verified via the AFE1 battle-init
            # cell-copy, where bank=4 src=$AA38(=$A57E+off) dst=frame-local len=1).
            # NB the params sit immediately after the task id — the earlier offsets
            # (src=sp+6/dst=sp+8/len=sp+10) skipped `src` by one word, which read a
            # bogus 60KB length and scribbled over the $05xx VM stack (→ $EF76 crash).
            bank, src, dst, n = rd(sp + 2), rw(sp + 4), rw(sp + 6), rw(sp + 8)
            for i in range(n & 0xFFFF):
                self.mem.write((dst + i) & 0xFFFF, self.read_banked(bank, (src + i) & 0xFFFF))
        elif task == 0x11:      # rng_next: advance the LCG and RETURN the value in regL.
            # rng_next_wrapper ($CA46) is just `push $11; syscall; RETURN` — it relies on
            # the syscall leaving the random word in regL (the $E9 plumbing copies a non-None
            # stub return into regL). The old stub only wrote brk_scratch ($66/$67) and
            # returned None, so regL kept a leftover and rng_mod's SIGNED `SMOD` produced
            # negatives (e.g. -13752 smod 10 = -2). Mask to 15 bits so the value is always
            # non-negative as signed -> rng_mod(n) yields a clean 0..n-1 for every modulus
            # the game uses (<= ~2025). brk_scratch is kept in sync for any direct readers.
            self._rng = (self._rng * 1103515245 + 12345) & 0xFFFF
            val = self._rng & 0x7FFF
            self.mem.write(0x66, val & 0xFF); self.mem.write(0x67, val >> 8)
            self.syscall_log.append((task, name, kind))
            return val
        elif task == 0x03:      # set_sprite: OAM shadow $0600+idx*4 = [Y, tile, attr, X]
            o = 0x0600 + (rd(sp + 2) & 0x3F) * 4
            self.mem.write(o, rd(sp + 6)); self.mem.write(o + 1, rd(sp + 0x0A))
            self.mem.write(o + 2, rd(sp + 8)); self.mem.write(o + 3, rd(sp + 4))
        elif task == 0x04:      # palette_write: palette_shadow $0700+idx
            self.mem.write(0x0700 + (rd(sp + 2) & 0x1F), rd(sp + 4))
        elif task == 0x12:      # palette_swap: $0700-$071F <-> $0090-$00AF
            for i in range(0x20):
                a, c = self.mem.read(0x0700 + i), self.mem.read(0x0090 + i)
                self.mem.write(0x0700 + i, c); self.mem.write(0x0090 + i, a)
        elif task == 0x09:      # audio_load_voice: 3 bytes at $0725 + voice*3
            o = 0x0725 + rd(sp + 2) * 3
            self.mem.write(o, rd(sp + 4)); self.mem.write(o + 1, rd(sp + 6))
            self.mem.write(o + 2, rd(sp + 8))
        elif task == 0x07:      # call_bank: switch to bank [sp+2], run its native
            # $8000 entry, restore bank. Mirrors native $C339 (save $0073 / set bank /
            # jsr $8000 / restore). The bank arg is at sp+2 (verified empirically: the
            # AFE1 battle-init call passes bank=10, an audio bank whose $8000 trampolines
            # via $E823 into inline VM bytecode). We must run that inline VM through our
            # own dispatcher loop so ITS $F226 syscalls hit this python layer, not the
            # MMC1-dependent native handlers. The effect (audio/PPU) is headless-moot;
            # what matters is leaving regL + the VM stack/frame as hardware would, so the
            # caller's subsequent vm_return doesn't derail (the old no-op stub jumped a
            # later return into the $EF76 frame-marker padding).
            bank = rd(sp + 2)
            saved_bank = self.mem.bank_lo
            self.switch_bank(bank)
            self._run_native_call(0x8000)
            self.switch_bank(saved_bank)
        # STUB / TODO: no headless memory effect — recorded only. (controller
        # returns 0 = no input by leaving brk_scratch untouched; PPU/audio moot.)
        self.syscall_log.append((task, name, kind))
        return None  # $E9 stub plumbing pops the adj byte + advances vm_pc

    # CPU PC sentinel used to detect return from a nested native call. Picked in
    # the fixed bank-15 vector tail; never a legitimate instruction-fetch target
    # (we test for it BEFORE fetching, so it is never executed).
    NATIVE_CALL_SENTINEL = 0xFFF9

    def _run_native_call(self, entry, max_steps=2_000_000):
        """Run native 6502 code from `entry` until it RTSs back to a sentinel,
        intercepting $F226 host_calls at the VM dispatcher boundary exactly as
        step_one_vm_op does (so a switched-in bank's inline-VM syscalls resolve
        through the python kernel layer instead of the MMC1-dependent native
        handlers). Used by call_bank ($07).

        Simulates `jsr entry`: pushes the sentinel as the return address, so the
        whole native+VM call chain unwinds back to us. Restores cpu.pc to the
        dispatcher on exit so the OUTER step_one_vm_op stays consistent.
        Returns True if it returned cleanly, False on step-budget exhaustion.
        """
        sent = self.NATIVE_CALL_SENTINEL
        self.cpu.push_word((sent - 1) & 0xFFFF)  # jsr pushes (return_addr - 1)
        self.cpu.pc = entry
        ok = False
        for _ in range(max_steps):
            pc = self.cpu.pc
            if pc == sent:
                ok = True
                break
            if pc == self.DISPATCHER_ADDR:
                vpc = self.vm_pc
                if self.mem.read(vpc) == 0xE9:  # CALL_abs_imm1 — maybe a host_call
                    tgt = self.mem.read(vpc + 1) | (self.mem.read(vpc + 2) << 8)
                    if tgt in self.host_call_stubs:
                        res = self.host_call_stubs[tgt](self)
                        if res is not None:
                            self.regL = res & 0xFFFF
                        adj = self.mem.read(vpc + 3)
                        self.vm_sp = (self.vm_sp + adj) & 0xFFFF
                        self.vm_pc = (vpc + 4) & 0xFFFF
                        continue
            self.cpu.step()
        # Leave the CPU at the dispatcher boundary for the caller's outer loop.
        self.cpu.pc = self.DISPATCHER_ADDR
        return ok


if __name__ == "__main__":
    # Smoke test: load ROM, run a few quick-immediate opcodes
    vm = NobunagaVM()
    print(f"Loaded {len(vm.labels)} labels")
    print(f"Mesen labels for selected addrs:")
    for addr in [0x87F0, 0xCA52, 0xA26F]:
        print(f"  {vm.fmt_addr(addr)}")

    # Place test bytecode in RAM: $4A $47 $4F $CF (setA=10, setA=7, setA=15, RETURN)
    vm.mem.write(0x0700, 0x4A)
    vm.mem.write(0x0701, 0x47)
    vm.mem.write(0x0702, 0x4F)
    vm.vm_pc = 0x0700
    vm.cpu.pc = vm.DISPATCHER_ADDR

    vm.trace_enabled = True
    vm.run_n_ops(3)
    print("\nTrace:")
    for line in vm.trace_log:
        print(line)
