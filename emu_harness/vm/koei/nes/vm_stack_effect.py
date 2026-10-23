"""Sea-16 VM per-opcode STACK_EFFECT table — the single authority (Pass 2, Layer 1 / M2).

This is the declarative model of what each VM opcode does to the data stack and the
register file. Until M2 it lived implicitly, scattered across the 350-line if/elif in
`vm_decompile.py` and (partially) in `nobunaga_vm.OPCODE_INFO` + `vm-opcodes-v2.toml`,
with the three never cross-checked. The Pass-1 `$30-$3F` bug (modeled as "store regB
to a frame slot" when it is really "PUSH a frame slot's value") silently corrupted 996
sites precisely because nobody validated the stack effect against the live handler.

`tools/vm-stack-audit.py` drives the REAL ROM handlers (via `nobunaga_vm.step_one_vm_op`)
over every opcode occurrence in the 4 code banks and asserts the observed SP / regL /
regR / frame-pointer / call-depth deltas match THIS table. The audit is the proof; this
table is the claim. (M3 will make `vm_decompile.py` read this table instead of its
inlined dispatch — but M2 is pure validation and does not touch the decompiler.)

Stack model (from vm-opcodes-v2.toml header): a DESCENDING word stack at $02. A push
decrements VM SP by 2, a pop increments it by 2. So for a pure data-stack opcode the
observed `SP_after - SP_before == 2 * (pops - pushes)`. regL is $08, regR is $0C.

Effect fields
-------------
  mnem    : OPCODE_INFO mnemonic (the spec name; for reference/reporting).
  cls     : how the audit checks it —
              'data'     reg/arith/load: gate dSP == 2*(pops-pushes) + 'keep' regs unchanged
              'push'     pushes a value:        "         (pushes>=1)
              'pop'      pops into a reg:        "         (pops>=1)
              'store'    writes mem/frame:       "         (regs kept)
              'control'  jump/branch/switch: gate dSP == 0 + regs kept (PC change is the point)
              'stackadj' $AE/$AF: gate dSP == +operand (pop N bytes)
              'call'     CALL family: gate vm_call_depth (+1 real call, 0 if host-stubbed)
              'return'   $CF: gate vm_call_depth -1 (frame teardown — dSP not asserted)
              'probe'    report-only, NOT gated: genuinely-ambiguous / unmodeled / dead /
                         syscall-trampoline / ext-op-prefix opcodes. The audit observes and
                         prints them so we can resolve them, but a probe can't fail the gate.
  pops    : data-stack words consumed (gated classes only).
  pushes  : data-stack words produced.
  regL    : 'w' the opcode writes regL, 'k' it must keep regL unchanged.
  regR    : 'w' / 'k' as above.
  vcheck  : optional value assertion the audit runs (a tag; see vm-stack-audit.py):
              'qimm_L'  regL_after == (op & 0x0F)
              'qimm_R'  regR_after == (op & 0x0F)
              'qpush'   top-of-stack word == (op & 0x0F)
              'absL'    regL_after == mem_word[operand]   (load reads the right address)
              'absS'    mem_word[operand] == regL_before  (store writes regL to the address)

Why several opcodes are 'probe' (report-only), not gated
  - $B2: OPCODE_INFO said NOP, vm_decompile.py treated it as PUSHL(regL). RESOLVED
         2026-06-03 = NOP: it has 0 occurrences in the code banks, so a synthetic
         micro-test (vm-stack-audit.py `validate_nop_b2`) ran one $B2 through the real
         ROM handler — dSP=0, regL/regR unchanged, pc+1. OPCODE_INFO/v2 toml were right;
         the decompiler's PUSHL was wrong (harmless: never emitted). Left 'probe' (report-
         only) because it can't be occurrence-gated, but the synthetic test re-proves it.
  - $B7 (LONG/ext_op): a PREFIX whose effect depends on the following index byte (32-bit
         ops on the reg file); audited per-index, reported, not gated as one opcode.
  - $80/$91-$9F/$CE/$EC.. : syscall trampolines into the kernel ($F226, stubbed) — effect
         is the syscall's, not a stack primitive.
  - $AD COPY_imm2, $E0-$E2 bitfield, $CC COMPL, $FF: present in the spec but not modeled in
         the decompiler dispatch; observe before claiming.
  - $A7 BYTE_LOADR_abs: 0 sites in the ROM (dead) — can't be exercised; logged as such.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Effect:
    mnem: str
    cls: str
    pops: int = 0
    pushes: int = 0
    regL: str = 'k'          # 'w' = written, 'k' = must keep
    regR: str = 'k'
    vcheck: Optional[str] = None

    @property
    def expected_dsp(self):
        """Net VM-SP delta for a gated data-stack opcode (descending word stack)."""
        return 2 * (self.pops - self.pushes)


GATED_CLASSES = frozenset({'data', 'push', 'pop', 'store', 'control'})

STACK_EFFECT = {}


def _r(a, b, eff):
    for op in range(a, b + 1):
        STACK_EFFECT[op] = eff


def _o(op, eff):
    STACK_EFFECT[op] = eff


def _build():
    # ---- Quick groups $00-$7F (operand in low nibble) --------------------------
    _r(0x00, 0x0B, Effect('LOADL_quick', 'data', regL='w'))   # regL = local[fast(op)]
    _r(0x0C, 0x0F, Effect('LOADL_quick', 'data', regL='w'))   # regL = arg[fast(op)]
    _r(0x10, 0x1B, Effect('LOADR_quick', 'data', regR='w'))   # regR = local
    _r(0x1C, 0x1F, Effect('LOADR_quick', 'data', regR='w'))   # regR = arg
    _r(0x20, 0x2B, Effect('STORE_quick', 'store'))            # local = regL
    _r(0x2C, 0x2F, Effect('STORE_quick', 'store'))            # arg   = regL
    _r(0x30, 0x3B, Effect('PUSH_quick', 'push', pushes=1))    # push local  (the $30-$3F fix)
    _r(0x3C, 0x3F, Effect('PUSH_quick', 'push', pushes=1))    # push arg
    _r(0x40, 0x4F, Effect('LOADL_qimm', 'data', regL='w', vcheck='qimm_L'))  # regL = op&0xF
    _o(0x50,       Effect('LOADR_qimm', 'data', regR='w', vcheck='qimm_R'))  # regR = 0
    _r(0x51, 0x5F, Effect('LOADR_qimm', 'data', regR='w', vcheck='qimm_R'))  # regR = op&0xF
    _r(0x60, 0x6F, Effect('PUSH_qimm', 'push', pushes=1, vcheck='qpush'))    # push op&0xF
    _o(0x70,       Effect('ADD_qimm', 'data', regL='w'))      # regL += 0 (NOP-ish; value kept)
    _r(0x71, 0x7F, Effect('ADD_qimm', 'data', regL='w'))      # regL += op&0xF

    # ---- $80 reserved/syscall, $81-$90 near/far/imm ----------------------------
    _o(0x80, Effect('ILLEGAL', 'probe'))                      # syscall-80 trampoline
    _o(0x81, Effect('LOADL_near', 'data', regL='w'))          # regL = frame_word(sbyte)
    _o(0x82, Effect('LOADL_far', 'data', regL='w'))           # regL = frame_word(sword)
    _o(0x83, Effect('LOADR_near', 'data', regR='w'))
    _o(0x84, Effect('LOADR_far', 'data', regR='w'))
    _o(0x85, Effect('STORE_near', 'store'))                   # frame_word(sbyte) = regL
    _o(0x86, Effect('STORE_far', 'store'))
    _o(0x87, Effect('PUSH_near', 'push', pushes=1))           # push frame_word(sbyte)
    _o(0x88, Effect('PUSH_far', 'push', pushes=1))
    _o(0x89, Effect('BYTE_LOADL_imm1', 'data', regL='w'))     # regL = sign-ext byte
    _o(0x8A, Effect('LOADL_imm2', 'data', regL='w'))          # regL = word imm
    _o(0x8B, Effect('BYTE_LOADR_imm1', 'data', regR='w'))
    _o(0x8C, Effect('LOADR_imm2', 'data', regR='w'))
    _o(0x8D, Effect('BYTE_PUSH_imm1', 'push', pushes=1))      # push sbyte imm
    _o(0x8E, Effect('PUSH_imm2', 'push', pushes=1))           # push word imm
    _o(0x8F, Effect('BYTE_ADD_imm1', 'data', regL='w'))       # regL += sbyte
    _o(0x90, Effect('ADD_imm2', 'data', regL='w'))            # regL += word

    # ---- $A0-$A3 byte far load/store/push --------------------------------------
    _o(0xA0, Effect('BYTE_LOADL_far', 'data', regL='w'))
    _o(0xA1, Effect('BYTE_LOADR_far', 'data', regR='w'))
    _o(0xA2, Effect('BYTE_STORE_far', 'store'))
    _o(0xA3, Effect('BYTE_PUSH_far', 'push', pushes=1))
    # ---- $A4-$AB absolute memory family ----------------------------------------
    _o(0xA4, Effect('LOADL_abs', 'data', regL='w', vcheck='absL'))   # regL = mem_word[abs]
    _o(0xA5, Effect('BYTE_LOADL_abs', 'data', regL='w'))             # regL = mem_byte[abs]
    _o(0xA6, Effect('LOADR_abs', 'data', regR='w'))
    _o(0xA7, Effect('BYTE_LOADR_abs', 'probe'))                      # 0 sites — dead
    _o(0xA8, Effect('STORE_abs', 'store', vcheck='absS'))            # mem_word[abs] = regL
    _o(0xA9, Effect('BYTE_STORE_abs', 'store'))                      # mem_byte[abs] = regL
    _o(0xAA, Effect('PUSH_abs', 'push', pushes=1))                   # push mem_word[abs]
    _o(0xAB, Effect('BYTE_PUSH_abs', 'push', pushes=1))             # push mem_byte[abs]
    # ---- $AC-$AF calls / copy / stack-adjust -----------------------------------
    _o(0xAC, Effect('CALL_abs', 'call'))                            # call inline target, no adj
    _o(0xAD, Effect('COPY_imm2', 'probe'))                          # len 4, unmodeled
    _o(0xAE, Effect('UNSTACK_imm1', 'probe'))   # SP += sbyte operand — exact relation TBD by audit
    _o(0xAF, Effect('UNSTACK_imm2', 'probe'))   # SP += word operand  — promote to a gate once confirmed
    # ---- $B0-$BF deref / stack / arithmetic ------------------------------------
    _o(0xB0, Effect('DEREF', 'data', regL='w'))                    # regL = *(word*)regL
    # POPSTORE pops the destination pointer INTO regR, then stores regL through it
    # (audit-confirmed: regR is clobbered — it is NOT a 'keep' register here).
    _o(0xB1, Effect('POPSTORE', 'store', pops=1, regR='w'))        # *(word*)pop()=regL (ptr->regR)
    _o(0xB2, Effect('NOP', 'probe'))                               # RESOLVED 2026-06-03: NOP (synthetic micro-test, dSP=0/regs kept) — see validate_nop_b2
    _o(0xB3, Effect('PUSHL', 'push', pushes=1))                    # push regL
    _o(0xB4, Effect('POPR', 'pop', pops=1, regR='w'))             # regR = pop()
    _o(0xB5, Effect('MULT', 'data', regL='w'))                    # regL = regL * regR
    _o(0xB6, Effect('SDIV', 'data', regL='w'))
    _o(0xB7, Effect('LONG', 'probe'))                             # ext-op prefix (per-index)
    _o(0xB8, Effect('UDIV', 'data', regL='w'))
    _o(0xB9, Effect('SMOD', 'data', regL='w'))
    _o(0xBA, Effect('UMOD', 'data', regL='w'))
    _o(0xBB, Effect('ADD', 'data', regL='w'))
    _o(0xBC, Effect('SUB', 'data', regL='w'))
    # Shifts use regR as a decrementing shift COUNTER (consumed to 0) — regR is clobbered,
    # not preserved (audit-confirmed). regL holds the result.
    _o(0xBD, Effect('LSHIFT', 'data', regL='w', regR='w'))        # regL <<= regR
    _o(0xBE, Effect('URSHIFT', 'data', regL='w', regR='w'))
    _o(0xBF, Effect('SRSHIFT', 'data', regL='w', regR='w'))
    # ---- $C0-$CF compares / unary / swap / return ------------------------------
    for op, mn in ((0xC0, 'CMPEQ'), (0xC1, 'CMPNE'), (0xC2, 'SCMPLT'), (0xC3, 'SCMPLE'),
                   (0xC4, 'SCMPGT'), (0xC5, 'SCMPGE'), (0xC6, 'UCMPLT'), (0xC7, 'UCMPLE'),
                   (0xC8, 'UCMPGT'), (0xC9, 'UCMPGE')):
        _o(op, Effect(mn, 'data', regL='w'))                     # regL = (regL ? regR)
    _o(0xCA, Effect('NOT', 'data', regL='w'))                    # regL = (regL == 0)
    _o(0xCB, Effect('MINUS', 'data', regL='w'))                  # regL = -regL
    _o(0xCC, Effect('COMPL', 'probe'))                           # unmodeled in decompiler
    _o(0xCD, Effect('SWAP', 'data', regL='w', regR='w'))         # regL <-> regR
    _o(0xCE, Effect('ILLEGAL', 'probe'))                         # syscall-CE trampoline
    _o(0xCF, Effect('RETURN', 'return'))                         # frame teardown, depth -1
    # ---- $D0-$DF inc/dec/shift / deref-byte / control / logic / call-ptr -------
    _o(0xD0, Effect('INC', 'data', regL='w'))
    _o(0xD1, Effect('DEC', 'data', regL='w'))
    _o(0xD2, Effect('LSHIFT1', 'data', regL='w'))                # regL <<= 1
    _o(0xD3, Effect('BYTE_DEREF', 'data', regL='w'))            # regL = *(byte*)regL
    _o(0xD4, Effect('BYTE_POPSTORE', 'store', pops=1, regR='w'))  # *(byte*)pop()=regL (ptr->regR)
    # Table-switches read the selector (regL) and SCRATCH both regs while indexing the
    # jump table (audit-confirmed); the gate holds them to dSP==0, not reg preservation.
    _o(0xD5, Effect('SWITCH_contig', 'control', regL='w', regR='w'))   # reads regL, jumps
    _o(0xD6, Effect('JUMP_abs', 'control'))
    _o(0xD7, Effect('JUMPT_abs', 'control'))
    _o(0xD8, Effect('JUMPF_abs', 'control'))
    _o(0xD9, Effect('SWITCH_noncontig', 'control', regL='w', regR='w'))
    _o(0xDA, Effect('AND', 'data', regL='w'))
    _o(0xDB, Effect('OR', 'data', regL='w'))
    _o(0xDC, Effect('XOR', 'data', regL='w'))
    _o(0xDD, Effect('CALLPTR', 'call'))                         # call through regL, no adj
    _o(0xDE, Effect('LEAL_far', 'data', regL='w'))             # regL = &frame_word
    _o(0xDF, Effect('LEAR_far', 'data', regR='w'))
    # ---- $E0-$EA bitfield / relative control / calls ---------------------------
    _o(0xE0, Effect('SLOADBF', 'probe'))                       # bitfield ops — unmodeled
    _o(0xE1, Effect('ULOADBF', 'probe'))
    _o(0xE2, Effect('STOREBF', 'probe'))
    _o(0xE3, Effect('JUMP_back', 'control'))
    _o(0xE4, Effect('JUMPT_back', 'control'))
    _o(0xE5, Effect('JUMPF_back', 'control'))
    _o(0xE6, Effect('JUMP_ahead', 'control'))
    _o(0xE7, Effect('JUMPT_ahead', 'control'))
    _o(0xE8, Effect('JUMPF_ahead', 'control'))
    _o(0xE9, Effect('CALL_abs_imm1', 'call'))                  # call + adj byte (host-stub aware)
    _o(0xEA, Effect('CALLPTR_imm1', 'call'))                   # call through regL + adj
    # ---- $EB-$FF kernel-syscall trampolines + tail ------------------------------
    for op in (0xEC, 0xED, 0xEE, 0xEF, 0xF2, 0xF3, 0xF4, 0xF6, 0xF7,
               0xF9, 0xFA, 0xFB, 0xFE):
        _o(op, Effect('SYSCALL', 'probe'))
    _o(0xFF, Effect('op_FF', 'probe'))


_build()


def effect(op):
    """Return the Effect for an opcode byte, or None if the byte is not a known opcode
    (operand bytes never reach here — the disassembler tiles them away)."""
    return STACK_EFFECT.get(op)


def is_gated(op):
    e = STACK_EFFECT.get(op)
    return bool(e and e.cls in GATED_CLASSES)


if __name__ == "__main__":
    # Quick census so the table is inspectable without the emulator.
    from collections import Counter
    by_cls = Counter(e.cls for e in STACK_EFFECT.values())
    # 'probe' is the only truly ungated class; call/return/stackadj get their own
    # special-purpose gates in vm-stack-audit.py (depth / SP-by-operand).
    print(f"STACK_EFFECT: {len(STACK_EFFECT)} opcode entries")
    for cls in sorted(by_cls):
        gate = "report-only" if cls == 'probe' else "gated"
        print(f"  {cls:9} {by_cls[cls]:3} ({gate})")
