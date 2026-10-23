"""The NMOS 6502 as the NES runs it (the 2A03): every documented instruction, no decimal mode.

Moved from koei-nes `tools/koeivm/cpu6502.py` (MIGRATION.md row C), where it ran KOEI's VM handlers
straight from ROM, and completed with the 18 opcodes game-annotation's Mappy tracer had added in a
subclass. Verified against kevtris's nestest: every documented-opcode line of the golden log
(PC, A, X, Y, P, SP; `tools/nestest.py`). Not modelled: cycle timing (`cycles` counts
instructions), the undocumented opcodes (they raise NotImplementedError, as the disassembler treats
them as data), and decimal mode (the 2A03 has none; D is stored and pushed, never used).

The memory is any object with read / write / read_word / write_word; a console binding supplies
it (KOEI's banked bus stays in koei-nes for now). Interrupts: `nmi()`, `irq()` and BRK push the
real status byte (B set only for BRK) and vector through $FFFA / $FFFE.
"""

NMI_VECTOR, RESET_VECTOR, IRQ_VECTOR = 0xFFFA, 0xFFFC, 0xFFFE


class CPU6502:
    """The documented NMOS 6502 instruction set over a pluggable memory (see the module docstring).
    Flags live as booleans (c z i d v n); `p` packs and unpacks them as the status byte."""

    def __init__(self, mem):
        self.mem = mem
        self.a = 0
        self.x = 0
        self.y = 0
        self.sp = 0xFD
        self.pc = 0
        # Status flags as individual booleans (easier to read than packed P)
        self.c = False  # carry
        self.z = False  # zero
        self.i = True   # interrupt disable
        self.d = False  # decimal
        self.v = False  # overflow
        self.n = False  # negative

        # Trace / instrumentation
        self.host_call_handlers = {}  # addr -> python callable for stubbing native subs
        self.stop_at = set()          # addresses where execution halts
        self.cycles = 0
        self.trace = False

    # ---- helpers --------------------------------------------------------
    @property
    def p(self):
        """The status byte as PHP pushes it minus B: bit 5 always set, B (bit 4) clear."""
        return (0x20 | self.n << 7 | self.v << 6 | self.d << 3 | self.i << 2 | self.z << 1 | self.c)

    @p.setter
    def p(self, val):
        self.n = bool(val & 0x80); self.v = bool(val & 0x40)
        self.d = bool(val & 0x08); self.i = bool(val & 0x04)
        self.z = bool(val & 0x02); self.c = bool(val & 0x01)

    def interrupt(self, vector, pc, brk=False):
        """Push `pc` and the status byte (B set only for BRK), set I, jump through `vector`."""
        self.push_word(pc & 0xFFFF)
        self.push(self.p | (0x10 if brk else 0))
        self.i = True
        self.pc = self.mem.read_word(vector)

    def nmi(self):
        self.interrupt(NMI_VECTOR, self.pc)

    def irq(self):
        """A maskable interrupt: taken only while I is clear. -> whether it was taken."""
        if self.i:
            return False
        self.interrupt(IRQ_VECTOR, self.pc)
        return True

    def set_nz(self, val):
        val &= 0xFF
        self.z = (val == 0)
        self.n = bool(val & 0x80)

    def push(self, val):
        self.mem.write(0x100 + self.sp, val & 0xFF)
        self.sp = (self.sp - 1) & 0xFF

    def pull(self):
        self.sp = (self.sp + 1) & 0xFF
        return self.mem.read(0x100 + self.sp)

    def push_word(self, val):
        self.push((val >> 8) & 0xFF)
        self.push(val & 0xFF)

    def pull_word(self):
        lo = self.pull()
        hi = self.pull()
        return lo | (hi << 8)

    # ---- addressing modes ----------------------------------------------
    def addr_zp(self):
        a = self.mem.read(self.pc)
        self.pc += 1
        return a

    def addr_zp_x(self):
        a = (self.mem.read(self.pc) + self.x) & 0xFF
        self.pc += 1
        return a

    def addr_zp_y(self):
        a = (self.mem.read(self.pc) + self.y) & 0xFF
        self.pc += 1
        return a

    def addr_abs(self):
        a = self.mem.read(self.pc) | (self.mem.read(self.pc + 1) << 8)
        self.pc += 2
        return a

    def addr_abs_x(self):
        return (self.addr_abs() + self.x) & 0xFFFF

    def addr_abs_y(self):
        return (self.addr_abs() + self.y) & 0xFFFF

    def addr_ind_y(self):
        zp = self.addr_zp()
        base = self.mem.read(zp) | (self.mem.read((zp + 1) & 0xFF) << 8)
        return (base + self.y) & 0xFFFF

    def addr_ind_x(self):
        zp = (self.addr_zp() + self.x) & 0xFF
        return self.mem.read(zp) | (self.mem.read((zp + 1) & 0xFF) << 8)

    def imm_byte(self):
        v = self.mem.read(self.pc)
        self.pc += 1
        return v

    # ---- arithmetic helpers --------------------------------------------
    def _adc(self, val):
        carry_in = 1 if self.c else 0
        result = self.a + val + carry_in
        self.c = result > 0xFF
        # Overflow: signs of A and val are same, but sign of result differs
        self.v = bool(((self.a ^ result) & (val ^ result)) & 0x80)
        self.a = result & 0xFF
        self.set_nz(self.a)

    def _sbc(self, val):
        # SBC = ADC with one's complement
        self._adc(val ^ 0xFF)

    def _cmp(self, reg, val):
        result = (reg - val) & 0x1FF
        self.c = reg >= val
        self.set_nz(result & 0xFF)

    # ---- single step ---------------------------------------------------
    def step(self):
        if self.pc in self.host_call_handlers:
            # User-installed stub for native helper subs (e.g., rng_mod)
            self.host_call_handlers[self.pc](self)
            # Stubs are expected to return via RTS-equivalent (we just pop)
            self.pc = self.pull_word() + 1
            return

        if self.pc in self.stop_at:
            raise StopIteration(f"Stopped at ${self.pc:04X}")

        opcode = self.mem.read(self.pc)
        self.pc += 1
        self._dispatch(opcode)
        self.cycles += 1  # rough — real 6502 timing varies per instruction

    def _dispatch(self, op):
        # ---- LDA family -------------------------------------------------
        if op == 0xA9:  # LDA #imm
            self.a = self.imm_byte(); self.set_nz(self.a)
        elif op == 0xA5:  # LDA zp
            self.a = self.mem.read(self.addr_zp()); self.set_nz(self.a)
        elif op == 0xB5:  # LDA zp,X
            self.a = self.mem.read(self.addr_zp_x()); self.set_nz(self.a)
        elif op == 0xAD:  # LDA abs
            self.a = self.mem.read(self.addr_abs()); self.set_nz(self.a)
        elif op == 0xBD:  # LDA abs,X
            self.a = self.mem.read(self.addr_abs_x()); self.set_nz(self.a)
        elif op == 0xB9:  # LDA abs,Y
            self.a = self.mem.read(self.addr_abs_y()); self.set_nz(self.a)
        elif op == 0xA1:  # LDA (zp,X)
            self.a = self.mem.read(self.addr_ind_x()); self.set_nz(self.a)
        elif op == 0xB1:  # LDA (zp),Y
            self.a = self.mem.read(self.addr_ind_y()); self.set_nz(self.a)
        # ---- LDX family -------------------------------------------------
        elif op == 0xA2:  # LDX #imm
            self.x = self.imm_byte(); self.set_nz(self.x)
        elif op == 0xA6:  # LDX zp
            self.x = self.mem.read(self.addr_zp()); self.set_nz(self.x)
        elif op == 0xAE:  # LDX abs
            self.x = self.mem.read(self.addr_abs()); self.set_nz(self.x)
        # ---- LDY family -------------------------------------------------
        elif op == 0xA0:  # LDY #imm
            self.y = self.imm_byte(); self.set_nz(self.y)
        elif op == 0xA4:  # LDY zp
            self.y = self.mem.read(self.addr_zp()); self.set_nz(self.y)
        elif op == 0xAC:  # LDY abs
            self.y = self.mem.read(self.addr_abs()); self.set_nz(self.y)
        # ---- STA family -------------------------------------------------
        elif op == 0x85:  # STA zp
            self.mem.write(self.addr_zp(), self.a)
        elif op == 0x95:  # STA zp,X
            self.mem.write(self.addr_zp_x(), self.a)
        elif op == 0x8D:  # STA abs
            self.mem.write(self.addr_abs(), self.a)
        elif op == 0x9D:  # STA abs,X
            self.mem.write(self.addr_abs_x(), self.a)
        elif op == 0x99:  # STA abs,Y
            self.mem.write(self.addr_abs_y(), self.a)
        elif op == 0x91:  # STA (zp),Y
            self.mem.write(self.addr_ind_y(), self.a)
        elif op == 0x81:  # STA (zp,X)
            self.mem.write(self.addr_ind_x(), self.a)
        # ---- STX/STY ----------------------------------------------------
        elif op == 0x86:  # STX zp
            self.mem.write(self.addr_zp(), self.x)
        elif op == 0x8E:  # STX abs
            self.mem.write(self.addr_abs(), self.x)
        elif op == 0x84:  # STY zp
            self.mem.write(self.addr_zp(), self.y)
        elif op == 0x8C:  # STY abs
            self.mem.write(self.addr_abs(), self.y)
        # ---- Transfers --------------------------------------------------
        elif op == 0xAA: self.x = self.a; self.set_nz(self.x)         # TAX
        elif op == 0xA8: self.y = self.a; self.set_nz(self.y)         # TAY
        elif op == 0x8A: self.a = self.x; self.set_nz(self.a)         # TXA
        elif op == 0x98: self.a = self.y; self.set_nz(self.a)         # TYA
        elif op == 0xBA: self.x = self.sp; self.set_nz(self.x)        # TSX
        elif op == 0x9A: self.sp = self.x                             # TXS
        # ---- Stack ------------------------------------------------------
        elif op == 0x48: self.push(self.a)                            # PHA
        elif op == 0x68: self.a = self.pull(); self.set_nz(self.a)    # PLA
        elif op == 0x08: self.push(self.p | 0x10)                    # PHP (pushes B set)
        elif op == 0x28: self.p = self.pull()                        # PLP (B and bit 5 ignored)
        # ---- Arithmetic -------------------------------------------------
        elif op == 0x69: self._adc(self.imm_byte())                   # ADC #
        elif op == 0x65: self._adc(self.mem.read(self.addr_zp()))     # ADC zp
        elif op == 0x6D: self._adc(self.mem.read(self.addr_abs()))    # ADC abs
        elif op == 0x71: self._adc(self.mem.read(self.addr_ind_y()))  # ADC (zp),Y
        elif op == 0xE9: self._sbc(self.imm_byte())                   # SBC #
        elif op == 0xE5: self._sbc(self.mem.read(self.addr_zp()))     # SBC zp
        elif op == 0xED: self._sbc(self.mem.read(self.addr_abs()))    # SBC abs
        # ---- Logic ------------------------------------------------------
        elif op == 0x29: self.a &= self.imm_byte(); self.set_nz(self.a)  # AND #
        elif op == 0x25: self.a &= self.mem.read(self.addr_zp()); self.set_nz(self.a)
        elif op == 0x09: self.a |= self.imm_byte(); self.set_nz(self.a)  # ORA #
        elif op == 0x05: self.a |= self.mem.read(self.addr_zp()); self.set_nz(self.a)
        elif op == 0x49: self.a ^= self.imm_byte(); self.set_nz(self.a)  # EOR #
        elif op == 0x45: self.a ^= self.mem.read(self.addr_zp()); self.set_nz(self.a)
        # ---- Shifts -----------------------------------------------------
        elif op == 0x0A:  # ASL A
            self.c = bool(self.a & 0x80); self.a = (self.a << 1) & 0xFF; self.set_nz(self.a)
        elif op == 0x4A:  # LSR A
            self.c = bool(self.a & 1); self.a >>= 1; self.set_nz(self.a)
        elif op == 0x2A:  # ROL A
            new_c = bool(self.a & 0x80)
            self.a = ((self.a << 1) | (1 if self.c else 0)) & 0xFF
            self.c = new_c; self.set_nz(self.a)
        elif op == 0x6A:  # ROR A
            new_c = bool(self.a & 1)
            self.a = ((self.a >> 1) | (0x80 if self.c else 0)) & 0xFF
            self.c = new_c; self.set_nz(self.a)
        # Memory shifts (read-modify-write)
        elif op in (0x06, 0x16, 0x0E, 0x1E):  # ASL zp/zpX/abs/absX
            a = {0x06: self.addr_zp, 0x16: self.addr_zp_x,
                 0x0E: self.addr_abs, 0x1E: self.addr_abs_x}[op]()
            v = self.mem.read(a); self.c = bool(v & 0x80)
            v = (v << 1) & 0xFF; self.mem.write(a, v); self.set_nz(v)
        elif op in (0x46, 0x56, 0x4E, 0x5E):  # LSR zp/zpX/abs/absX
            a = {0x46: self.addr_zp, 0x56: self.addr_zp_x,
                 0x4E: self.addr_abs, 0x5E: self.addr_abs_x}[op]()
            v = self.mem.read(a); self.c = bool(v & 1)
            v = v >> 1; self.mem.write(a, v); self.set_nz(v)
        elif op in (0x26, 0x36, 0x2E, 0x3E):  # ROL zp/zpX/abs/absX
            a = {0x26: self.addr_zp, 0x36: self.addr_zp_x,
                 0x2E: self.addr_abs, 0x3E: self.addr_abs_x}[op]()
            v = self.mem.read(a); new_c = bool(v & 0x80)
            v = ((v << 1) | (1 if self.c else 0)) & 0xFF
            self.c = new_c; self.mem.write(a, v); self.set_nz(v)
        elif op in (0x66, 0x76, 0x6E, 0x7E):  # ROR zp/zpX/abs/absX
            a = {0x66: self.addr_zp, 0x76: self.addr_zp_x,
                 0x6E: self.addr_abs, 0x7E: self.addr_abs_x}[op]()
            v = self.mem.read(a); new_c = bool(v & 1)
            v = (v >> 1) | (0x80 if self.c else 0)
            self.c = new_c; self.mem.write(a, v); self.set_nz(v)
        # BIT
        elif op == 0x24:  # BIT zp
            v = self.mem.read(self.addr_zp())
            self.z = (self.a & v) == 0
            self.n = bool(v & 0x80); self.v = bool(v & 0x40)
        elif op == 0x2C:  # BIT abs
            v = self.mem.read(self.addr_abs())
            self.z = (self.a & v) == 0
            self.n = bool(v & 0x80); self.v = bool(v & 0x40)
        # More CMP/AND/ORA/EOR variants
        elif op == 0xC5: self._cmp(self.a, self.mem.read(self.addr_zp()))
        elif op == 0xD5: self._cmp(self.a, self.mem.read(self.addr_zp_x()))
        elif op == 0xCD: self._cmp(self.a, self.mem.read(self.addr_abs()))
        elif op == 0xDD: self._cmp(self.a, self.mem.read(self.addr_abs_x()))
        elif op == 0xD9: self._cmp(self.a, self.mem.read(self.addr_abs_y()))
        elif op == 0xD1: self._cmp(self.a, self.mem.read(self.addr_ind_y()))
        elif op == 0x35: self.a &= self.mem.read(self.addr_zp_x()); self.set_nz(self.a)
        elif op == 0x2D: self.a &= self.mem.read(self.addr_abs()); self.set_nz(self.a)
        elif op == 0x3D: self.a &= self.mem.read(self.addr_abs_x()); self.set_nz(self.a)
        elif op == 0x39: self.a &= self.mem.read(self.addr_abs_y()); self.set_nz(self.a)
        elif op == 0x31: self.a &= self.mem.read(self.addr_ind_y()); self.set_nz(self.a)
        elif op == 0x15: self.a |= self.mem.read(self.addr_zp_x()); self.set_nz(self.a)
        elif op == 0x0D: self.a |= self.mem.read(self.addr_abs()); self.set_nz(self.a)
        elif op == 0x1D: self.a |= self.mem.read(self.addr_abs_x()); self.set_nz(self.a)
        elif op == 0x19: self.a |= self.mem.read(self.addr_abs_y()); self.set_nz(self.a)
        elif op == 0x11: self.a |= self.mem.read(self.addr_ind_y()); self.set_nz(self.a)
        elif op == 0x55: self.a ^= self.mem.read(self.addr_zp_x()); self.set_nz(self.a)
        elif op == 0x4D: self.a ^= self.mem.read(self.addr_abs()); self.set_nz(self.a)
        elif op == 0x5D: self.a ^= self.mem.read(self.addr_abs_x()); self.set_nz(self.a)
        elif op == 0x59: self.a ^= self.mem.read(self.addr_abs_y()); self.set_nz(self.a)
        elif op == 0x51: self.a ^= self.mem.read(self.addr_ind_y()); self.set_nz(self.a)
        elif op == 0x75: self._adc(self.mem.read(self.addr_zp_x()))
        elif op == 0x7D: self._adc(self.mem.read(self.addr_abs_x()))
        elif op == 0x79: self._adc(self.mem.read(self.addr_abs_y()))
        elif op == 0xF5: self._sbc(self.mem.read(self.addr_zp_x()))
        elif op == 0xFD: self._sbc(self.mem.read(self.addr_abs_x()))
        elif op == 0xF9: self._sbc(self.mem.read(self.addr_abs_y()))
        elif op == 0xF1: self._sbc(self.mem.read(self.addr_ind_y()))   # SBC (zp),Y
        elif op == 0xE1: self._sbc(self.mem.read(self.addr_ind_x()))   # SBC (zp,X)
        # ---- Compare ----------------------------------------------------
        elif op == 0xC9: self._cmp(self.a, self.imm_byte())           # CMP #
        elif op == 0xE0: self._cmp(self.x, self.imm_byte())           # CPX #
        elif op == 0xE4: self._cmp(self.x, self.mem.read(self.addr_zp()))
        elif op == 0xC0: self._cmp(self.y, self.imm_byte())           # CPY #
        elif op == 0xC4: self._cmp(self.y, self.mem.read(self.addr_zp()))
        # ---- Inc/Dec ----------------------------------------------------
        elif op == 0xE8: self.x = (self.x + 1) & 0xFF; self.set_nz(self.x)  # INX
        elif op == 0xCA: self.x = (self.x - 1) & 0xFF; self.set_nz(self.x)  # DEX
        elif op == 0xC8: self.y = (self.y + 1) & 0xFF; self.set_nz(self.y)  # INY
        elif op == 0x88: self.y = (self.y - 1) & 0xFF; self.set_nz(self.y)  # DEY
        elif op == 0xE6:  # INC zp
            a = self.addr_zp(); v = (self.mem.read(a) + 1) & 0xFF; self.mem.write(a, v); self.set_nz(v)
        elif op == 0xEE:  # INC abs
            a = self.addr_abs(); v = (self.mem.read(a) + 1) & 0xFF; self.mem.write(a, v); self.set_nz(v)
        elif op == 0xC6:  # DEC zp
            a = self.addr_zp(); v = (self.mem.read(a) - 1) & 0xFF; self.mem.write(a, v); self.set_nz(v)
        elif op == 0xCE:  # DEC abs
            a = self.addr_abs(); v = (self.mem.read(a) - 1) & 0xFF; self.mem.write(a, v); self.set_nz(v)
        # ---- Branches ---------------------------------------------------
        elif op in (0x10, 0x30, 0x50, 0x70, 0x90, 0xB0, 0xD0, 0xF0):
            offset = self.imm_byte()
            if offset >= 0x80: offset -= 0x100
            cond = {
                0x10: not self.n,  # BPL
                0x30: self.n,      # BMI
                0x50: not self.v,  # BVC
                0x70: self.v,      # BVS
                0x90: not self.c,  # BCC
                0xB0: self.c,      # BCS
                0xD0: not self.z,  # BNE
                0xF0: self.z,      # BEQ
            }[op]
            if cond: self.pc = (self.pc + offset) & 0xFFFF
        # ---- Flag ops ---------------------------------------------------
        elif op == 0x18: self.c = False        # CLC
        elif op == 0x38: self.c = True         # SEC
        elif op == 0xD8: self.d = False        # CLD
        elif op == 0xF8: self.d = True         # SED
        elif op == 0x58: self.i = False        # CLI
        elif op == 0x78: self.i = True         # SEI
        elif op == 0xB8: self.v = False        # CLV
        # ---- Control flow -----------------------------------------------
        elif op == 0x4C:  # JMP abs
            self.pc = self.addr_abs()
        elif op == 0x6C:  # JMP (abs)
            addr = self.addr_abs()
            # Note: 6502 has a bug where JMP (abs) doesn't cross page boundaries;
            # we replicate it for fidelity.
            lo = self.mem.read(addr)
            hi = self.mem.read((addr & 0xFF00) | ((addr + 1) & 0xFF))
            self.pc = lo | (hi << 8)
        elif op == 0x20:  # JSR abs
            target = self.addr_abs()
            # Push return address - 1 (6502 convention)
            self.push_word((self.pc - 1) & 0xFFFF)
            self.pc = target
        elif op == 0x60:  # RTS
            self.pc = (self.pull_word() + 1) & 0xFFFF
        elif op == 0x40:  # RTI
            self.p = self.pull()
            self.pc = self.pull_word()
        elif op == 0x00:  # BRK: skips a padding byte, vectors through $FFFE with B set
            self.interrupt(IRQ_VECTOR, self.pc + 1, brk=True)
        elif op == 0xEA:  # NOP
            pass
        # ---- the 18 the KOEI VM never used (from game-annotation's Mappy tracer) ----
        elif op == 0x01: self.a |= self.mem.read(self.addr_ind_x()); self.set_nz(self.a)   # ORA (zp,X)
        elif op == 0x21: self.a &= self.mem.read(self.addr_ind_x()); self.set_nz(self.a)   # AND (zp,X)
        elif op == 0x41: self.a ^= self.mem.read(self.addr_ind_x()); self.set_nz(self.a)   # EOR (zp,X)
        elif op == 0x61: self._adc(self.mem.read(self.addr_ind_x()))                       # ADC (zp,X)
        elif op == 0xC1: self._cmp(self.a, self.mem.read(self.addr_ind_x()))               # CMP (zp,X)
        elif op == 0x94: self.mem.write(self.addr_zp_x(), self.y)                          # STY zp,X
        elif op == 0x96: self.mem.write(self.addr_zp_y(), self.x)                          # STX zp,Y
        elif op == 0xB4: self.y = self.mem.read(self.addr_zp_x()); self.set_nz(self.y)     # LDY zp,X
        elif op == 0xB6: self.x = self.mem.read(self.addr_zp_y()); self.set_nz(self.x)     # LDX zp,Y
        elif op == 0xBC: self.y = self.mem.read(self.addr_abs_x()); self.set_nz(self.y)    # LDY abs,X
        elif op == 0xBE: self.x = self.mem.read(self.addr_abs_y()); self.set_nz(self.x)    # LDX abs,Y
        elif op == 0xCC: self._cmp(self.y, self.mem.read(self.addr_abs()))                 # CPY abs
        elif op == 0xEC: self._cmp(self.x, self.mem.read(self.addr_abs()))                 # CPX abs
        elif op == 0xD6:  # DEC zp,X
            a = self.addr_zp_x(); v = (self.mem.read(a) - 1) & 0xFF; self.mem.write(a, v); self.set_nz(v)
        elif op == 0xF6:  # INC zp,X
            a = self.addr_zp_x(); v = (self.mem.read(a) + 1) & 0xFF; self.mem.write(a, v); self.set_nz(v)
        elif op == 0xDE:  # DEC abs,X
            a = self.addr_abs_x(); v = (self.mem.read(a) - 1) & 0xFF; self.mem.write(a, v); self.set_nz(v)
        elif op == 0xFE:  # INC abs,X
            a = self.addr_abs_x(); v = (self.mem.read(a) + 1) & 0xFF; self.mem.write(a, v); self.set_nz(v)
        else:
            raise NotImplementedError(f"Unimplemented opcode ${op:02X} at PC=${self.pc-1:04X}")

    # ---- Convenience ---------------------------------------------------
    def run_until(self, stop_addrs, max_steps=1_000_000):
        """Run until PC hits any of stop_addrs, then return.
        Raises if max_steps exceeded.
        """
        stop = set(stop_addrs)
        for _ in range(max_steps):
            if self.pc in stop:
                return
            self.step()
        raise RuntimeError(f"Exceeded {max_steps} steps")
