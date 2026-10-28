"""
Sea-16 VM → C decompiler — prototype.

Strategy:
  1. Parse a sub from a bank's disassembly listing (the *_vm.asm files).
  2. Symbolically execute: regA, regB hold C *expressions* (strings).
     Data stack holds pushed expressions. Frame locals named arg1/local1/etc.
  3. Emit C when an opcode commits (vm_return, store, branch, host_call).
  4. Structured-control-flow lite: detect simple if/else patterns from forward
     branches that converge.

This is intentionally simple: linear pass with expression folding, branches
become labels + goto initially. Pattern-recognize if/else as a post-pass.

Usage:
  py -3 vm_decompile.py <bank_vm.asm> <sub_addr_hex>
  py -3 vm_decompile.py disasm/bank_01_vm.asm 87D8
"""
import sys
import re
from pathlib import Path


# --- Asset-ID enums (2026-06-10) --------------------------------------------
# Substitute named constants for the literal int args of "enum-typed" functions,
# so trigger_cutscene(26) renders as trigger_cutscene(CUTSCENE_BRIBE). Data lives
# in a title's repo next to its mesen-labels.toml (only NA1 has one so far), found through the
# title profile; set_schema(game) loads the title's own. The default is NA1's, for direct callers.
# (The path used to be computed from this file's location; after koeivm left na1-decompiler it
# pointed nowhere and the names silently dropped out. emu-harness MIGRATION.md row F1b.)
# Minimal parser — no external toml dependency.
def _load_asset_enums(game="na1"):
    from emu_harness.vm.koei.nes.titles import game_cfg
    p = Path(game_cfg(game)["_root"]) / "asset-enums.toml"
    arg_enums, enums = {}, {}
    if not p.exists():
        return arg_enums, enums
    section = None
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        sm = re.match(r'\[(\w+)\]', line)
        if sm:
            section = sm.group(1)
            continue
        if section == 'arg_enums':
            m = re.match(r'(\w+)\s*=\s*\{\s*(\d+)\s*=\s*"(\w+)"\s*\}', line)
            if m:
                arg_enums[m.group(1)] = {int(m.group(2)): m.group(3)}
        elif section:
            m = re.match(r'(\d+)\s*=\s*"(\w+)"', line)
            if m:
                enums.setdefault(section, {})[int(m.group(1))] = m.group(2)
    return arg_enums, enums

_ARG_ENUMS, _ENUMS = _load_asset_enums()

# --- ext-op body overrides: NA1's five 32-bit helpers ---------------------------------
# DREAM cannot fold the 32-bit aux-stack math, so these subs print a formula instead of their body.
# Each entry is (C lines, a model, the bank): the model is the same formula in Python, and
# gates.cfg_gate checks it against the ROM (titles.run_sub) over an edge grid, since an override
# has no CFG to compare. Re-derived 2026-10-27 from each sub's bytecode: the June 2026-06-10
# versions were unsigned and dropped the guards (wrong at every branch edge and at $8000).
# The primitives, as measured on NA1's handlers: B7 $25 sign-extends 16 -> 32, B7 $26 zero-extends,
# $01 is a 32-bit multiply, $02 is KOEI's sdiv32, which truncates toward zero but divides by
# |d| - 1 for a negative divisor (it one's-complements it) and gives -1 for a zero one, before the
# sign fix. The same sdiv32 is in na2, rot3k2, bk, lemp and uw. min_word is a signed min.
# These are NA1's: set_schema(game) turns them off for every other title (GK's bank-5 $8327 is
# different bytecode; MIGRATION.md row F1b).
_M32 = 0xFFFFFFFF


def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


def _sdiv32(a, d):
    a = (a & _M32) - ((a & 0x80000000) << 1)
    d = (d & _M32) - ((d & 0x80000000) << 1)
    q = _M32 if d in (0, -1) else abs(a) // (d if d > 0 else -d - 1)
    return (-q if (a < 0) != (d < 0) else q) & _M32


def _muladddiv(a, b):
    return _sdiv32(_s16(a) * _s16(b) + 9, 10) & 0xFFFF


_SDIV32_NOTE = "// sdiv32: KOEI's 32-bit signed divide; a negative divisor d divides by |d| - 1 (ROM bug)"
_EXTOP_OVERRIDES_NA1 = _EXTOP_OVERRIDE = {
    0x8303: (lambda p: [_SDIV32_NOTE,
                        f"return (word)sdiv32((sword){p[0]} * (sword){p[1]} + 9, 10);"],
             _muladddiv, 1),
    0x8327: (lambda p: [_SDIV32_NOTE,
                        f"if ((sword)math32_muladddiv({p[0]}, {p[1]}) > (sword){p[2]}) return 0xFFFF;",
                        f"return (word)sdiv32((sword){p[0]} * (sword){p[1]}, 10);"],
             lambda a, b, c: 0xFFFF if _s16(_muladddiv(a, b)) > _s16(c) else _sdiv32(_s16(a) * _s16(b), 10) & 0xFFFF, 1),
    0x8357: (lambda p: [_SDIV32_NOTE,
                        f"return min_word((word)sdiv32((sword){p[0]} * 10, (sword){p[1]}), {p[2]});"],
             lambda a, b, c: (lambda x: x if _s16(x) < _s16(c) else c)(_sdiv32(_s16(a) * 10, _s16(b)) & 0xFFFF), 1),
    0xD6B8: (lambda p: [_SDIV32_NOTE,
                        f"if (!{p[2]}) return 0xFFFF;",
                        f"return (word)sdiv32((sword){p[0]} * (sword){p[1]}, (sword){p[2]});"],
             lambda a, b, c: 0xFFFF if c == 0 else _sdiv32(_s16(a) * _s16(b), _s16(c)) & 0xFFFF, 15),
    0xD6DE: (lambda p: [f"if (!{p[0]} && !{p[1]}) return 0;",
                        f"return (word)(((dword){p[0]} * 100) / ((dword){p[0]} + {p[1]}));"],
             lambda a, b: 0 if a == 0 and b == 0 else _sdiv32(100 * a, a + b) & 0xFFFF, 15),
}

def na1_extop_model(stub):
    """The ROM-checked Python model of one of NA1's five ext-op helpers (by stub address), for
    callers that compute with them (na1sim) instead of copying a formula. Takes and returns 16-bit
    words, as the ROM does."""
    return _EXTOP_OVERRIDES_NA1[stub][1]


def _apply_arg_enums(fname, args):
    """Replace literal-int args of enum-typed functions with named constants."""
    emap = _ARG_ENUMS.get(fname)
    if not emap:
        return args
    for idx, etable in emap.items():
        if idx < len(args) and re.fullmatch(r'-?\d+', args[idx].strip()):
            const = _ENUMS.get(etable, {}).get(int(args[idx]))
            if const:
                args[idx] = const
    return args


# --- VM-model drift-guard (M3, 2026-06-03) ----------------------------------
# A self-check that the decompiler's per-opcode data-stack handling agrees with
# the AUDITED authority `vm_stack_effect.STACK_EFFECT` (which vm-stack-audit.py
# proves against the live ROM handler, M2). It would have caught both Pass-1/M2
# stack-model bugs: $30-$3F (modeled as a store, Δ0, vs PUSH, Δ+1) and $B2
# (modeled as PUSHL, Δ+1, vs NOP, Δ0). Drift-proof because it OBSERVES the real
# handlers (no hand-maintained parallel table): when SELF_CHECK is on, decompile()
# logs the data-stack DEPTH at the top of every instruction; check_stack_trace()
# turns those into per-opcode Δ and compares to the table. SELF_CHECK defaults
# False so the production decompile path (and decompiled/*.c) is byte-for-byte
# unaffected. The full "decompiler reads the table to DRIVE its dispatch" rewrite
# is deferred to the end-of-project VM teardown (ROADMAP); this guards against
# drift in the meantime. Run: `py -3 tools/decompile-all.py --self-check`.
SELF_CHECK = False
_STACK_TRACE = []        # appended (opcode|None, depth) during a SELF_CHECK decompile


# --- Rendering switch: structured C vs the raw goto witness (CFG epic, Phase 0) -
# Decision (B), Chris 2026-06-03: the READABLE structured C (if/while/switch + braces)
# is the on-by-default canonical artifact; the raw goto form is a producible WITNESS
# (`decompile-all.py --raw`) for the CFG round-trip / equivalence gate. STRUCTURE=True
# runs structure_lines() exactly as before, so decompiled/*.c stays byte-identical;
# STRUCTURE=False skips ALL structuring → the pure label+goto form that maps 1:1 to the
# bytecode CFG. The control-flow structuring epic crosses the project's reversibility
# line (text-level losslessness → behaviour-level CFG-equivalence) but NOT determinism;
# tools/vm-cfg-gate.py is the honesty gate. Mirrors the SELF_CHECK default-off pattern.
STRUCTURE = True


# --- Structurer selector: template menagerie (V1) vs region reducer (V2) --------
# CFG epic, REFRAME 2026-06-04: the template approach (structure_loops/structure_lines/
# structure_switches) was replaced by a single bottom-up CFG region reducer
# (tools/vm_reduce.py) that is closed under composition — see vm_reduce's docstring +
# the ROADMAP epic. CUTOVER 2026-06-10: DREAM (below) is now the canonical structurer and
# V2 is its per-sub FALLBACK; the V1 templates are RETIRED from the canonical emit path but
# kept in this module — the 114 soundness suite (test-vm-cfg.py) and the default vm-cfg-gate
# still exercise them as a front-end regression guard. STRUCTURE_V2=True selects the V2
# reducer alone (the --v2 audit/diff path); it feeds the SAME structured_equivalent gate.
STRUCTURE_V2 = False


# --- DREAM cutover (CFG epic, rung 6 EXECUTED 2026-06-10): DREAM is the PRIMARY
# structurer; V2 (region reducer) is retained as the per-sub FALLBACK. With
# STRUCTURE_DREAM=True the decompile() dispatch tries DREAM first (pattern-independent
# reaching-condition structurer, `tools/dream.py`), gate-validated by its AST-native
# `dream_equivalent_ast`; a sub DREAM can't own (out of scope / gate reject / honest
# over-dup) falls back to the V2 reducer, itself gated by structured_equivalent. So the
# canonical decompiled/*.c is DREAM where it wins (~479/495), V2 where it doesn't. The
# `structure_dream` hook is wired by the driver (decompile-all.py) to
# dream.dream_structure_gated — left None here to keep tools/vm_decompile import-light
# (dream.py is only pulled in on the canonical-emit path). Default OFF so the gate tools
# and captures keep their explicit-flag control.
STRUCTURE_DREAM = False
structure_dream = None      # set to dream.dream_structure_gated by the canonical driver


def structure_v2(lines, instructions):
    """The region-reducer structurer (CFG epic V2). Thin delegate to vm_reduce.reduce
    so the reducer lives in its own module; this keeps the gate-wiring in decompile()
    one symbol to swap when V2 dominates. Rung 0: passthrough (returns raw lines)."""
    from emu_harness.vm.koei.nes import vm_reduce
    return vm_reduce.reduce(lines, instructions)


# --- Opcode handlers: each takes (state, operand) and may emit C lines ---

class State:
    def __init__(self):
        self._regA = "?"
        self.pending_call = None  # a host_call/CALL expr parked in regA, not yet consumed
        self.pending_call_addr = 0  # its bytecode address (so a flushed stmt cites the call site)
        self.regB = "?"
        self.stack = []  # list of expressions
        self.locals = {}  # frame slot -> last expression assigned
        self.lines = []  # emitted C lines (with indent level + text)
        self.indent = 1
        self.labels_needed = set()  # branch targets that need labels
        self.cur_addr = 0

    # regA is a stack-machine accumulator. A CALL writes a call-expr into it (via
    # set_call) but emits no statement — it only surfaces in C if something READS regA
    # (store/return/branch/op) before the next WRITE. Without help, a side-effecting call
    # whose result is discarded (the next op overwrites regA) vanishes silently — which is
    # why vm_bootstrap's init_new_game_state()/audio/planner calls were invisible.
    # The property makes the dataflow explicit:
    #   - READ  (getter): the value is being used -> a pending call is CONSUMED (no extra stmt).
    #   - WRITE (setter): if a pending call is being overwritten unconsumed, it was a
    #                     statement-for-side-effects -> flush it as `expr;` before overwriting.
    @property
    def regA(self):
        self.pending_call = None
        return self._regA

    @regA.setter
    def regA(self, val):
        self.flush_pending()  # overwriting an unconsumed call -> emit it for its side effects
        self._regA = val

    def flush_pending(self):
        """Emit a parked-but-unconsumed call as a side-effect statement. Called on regA
        overwrite (setter) and at control points that don't read regA (unconditional goto,
        function end), so a discarded call is never silently lost. The statement is tagged
        with the CALL's own address, not the flush site."""
        if self.pending_call is not None:
            self.lines.append((self.pending_call_addr, self.indent, f"{self.pending_call};"))
            self.pending_call = None

    def set_call(self, expr):
        """Record a call: overwrite regA (flushing any prior dead call) and mark this one
        pending until it is consumed or itself overwritten."""
        self.regA = expr               # setter flushes a prior unconsumed call first
        self.pending_call = expr       # ...this one becomes the new pending call
        self.pending_call_addr = self.cur_addr

    def emit(self, line):
        self.lines.append((self.cur_addr, self.indent, line))


# Local frame slot naming convention:
#   loadA_local_pos with inline op 0x0C/0x0D/0x0E/0x0F → arg1/arg2/arg3/arg4
#   loadA_local_neg with inline op 0-11 → local0..local11
def local_name(opcode_low, kind):
    """kind is 'pos' (args, frame[+11/+13/+15/+17]) or 'neg' (locals)."""
    if kind == 'pos':
        # 0x0C = arg1, 0x0D = arg2, 0x0E = arg3, 0x0F = arg4
        return f"arg{opcode_low - 0x0B}"
    else:
        # 0x00..0x0B = local0..local11 (frame[-24+2*n])
        return f"local{opcode_low}"


# --- General frame-local access (signed-offset opcodes) ---
# Opcodes $81-$88 / $A0-$A3 / $90 / $DE-$DF address frame memory relative to the
# VM frame pointer vm_fp (decoded from the handlers at $EA34-$EB25: each does
# `jsr vm_addr_ptr2_plus_{sbyte,word}` -> effective addr = vm_fp + offset, then a
# load/store/push). The 12 standard WORD locals sit at vm_fp-24..vm_fp-2
# (local0..local11), so an offset landing on one renders as that name — identical
# to the quick load/store opcodes $00-$3F. Any other offset (odd byte locals, args
# above fp, big frames) renders as an explicit (fp +/- N) expression.
_WORD_SLOT = {(-24 + 2 * n): f"local{n}" for n in range(12)}


def _slot_name(off):
    """Resolve a word-aligned frame offset to its positional slot name, or None.
    Negative even offsets vm_fp-24..vm_fp-2 are local0..local11. Positive ODD
    offsets from +0x0B are the stack-passed args: arg1 = vm_fp+0x0B (=+11), then
    +2 bytes per word arg (arg2=+13, arg3=+15, arg4=+17, arg5=+19, argN=+0x0B+2*(N-1)).
    The quick opcodes $00-$3F only reach local0-11 + arg1-4; this ALSO names arg5+ and
    any arg/local reached by the GENERAL signed-offset frame opcodes ($81-$88, $DE/$DF),
    which previously rendered raw as `*(word*)(fp + N)`. (Positive offsets <0x0B are the
    frame header; even positives and odd <0x0B are not args -> None -> render raw.)"""
    name = _WORD_SLOT.get(off)
    if name:
        return name
    if off >= 0x0B and off % 2 == 1:
        return f"arg{(off - 0x0B) // 2 + 1}"
    return None


def _op_hex(operand):
    """First `$hex` literal in the operand as an unsigned int, else 0 — absolute
    addresses and word immediates (the repeated `re.search(r'\\$([0-9A-Fa-f]+)')` idiom)."""
    m = re.search(r'\$([0-9A-Fa-f]+)', operand)
    return int(m.group(1), 16) if m else 0


def _op_int(operand):
    """First signed-decimal literal in the operand as an int, else 0 — signed-byte/word
    immediates (the `re.search(r'(-?\\d+)')` idiom). NOTE: distinct from the positive-only
    `\\+?(\\d+)` the $89/$8B byte-imm arms use — those keep their own parse."""
    m = re.search(r'(-?\d+)', operand)
    return int(m.group(1)) if m else 0


def _signed_operand(operand):
    """Parse the `$XX`/`$XXXX` operand of a frame op as a signed byte/word.
    Width is taken from the hex-digit count vm-disasm.py emits (2 = signed byte,
    4 = signed word) — matches how the byte-offset ($81/83/85/87) vs word-offset
    ($82/84/86/88, $A0-A3, $90, $DE/DF) handlers fetch their operand."""
    m = re.search(r'\$([0-9A-Fa-f]+)', operand)
    if not m:
        return 0
    h = m.group(1)
    val = int(h, 16)
    bits = 4 * len(h)
    if bits and val >= (1 << (bits - 1)):
        val -= (1 << bits)
    return val


def _fp_disp(off):
    """Render a signed frame displacement as 'fp', 'fp + N', or 'fp - N'."""
    if off == 0:
        return "fp"
    return f"fp + {off}" if off > 0 else f"fp - {-off}"


def frame_word(off):
    """Word frame local at vm_fp+off (lvalue or rvalue)."""
    return _slot_name(off) or f"*(word*)({_fp_disp(off)})"


def frame_byte(off):
    """Byte frame local at vm_fp+off (lvalue or rvalue). If the offset lands on a named
    word slot (arg/local), render `*(byte*)&name` -- the slot's low byte (little-endian),
    valid as BOTH lvalue and rvalue (unlike a `(byte)name` cast) -- so byte views of a
    word arg (e.g. a char passed in arg1, read by the text engine) name the slot instead
    of a raw frame displacement. Offsets that miss a slot (high bytes, odd <0x0B) stay raw."""
    name = _slot_name(off)
    return f"*(byte*)&{name}" if name else f"*(byte*)({_fp_disp(off)})"


def frame_addr(off):
    """Address of a frame local at vm_fp+off."""
    name = _slot_name(off)
    return f"&{name}" if name else f"({_fp_disp(off)})"


# ============================================================================
# RECORD FIELD MAPS ARE PER-TITLE -- the KOEI games REUSE offsets with different meanings, so the field map
# is selected by game (set_schema). They live in the title profiles, games.toml [<title>.fields], with their
# provenance (moved there in emu-harness MIGRATION.md row M). Word fields annotate; byte stats stay raw.
# ============================================================================
def _fields_of(game):
    """(province_var, province map, arg map) from games.toml [<game>.fields], or None if it has none."""
    from emu_harness.vm.koei.nes.titles import load_games
    f = load_games().get(game, {}).get("fields")
    if f is None:
        return None
    province = {int(k): v for k, v in f.get("province", {}).items()}
    arg = province if f.get("arg") == "province" else {int(k): v for k, v in f.get("arg", {}).items()}
    return f.get("province_var", ""), province, arg


_DEFAULT_SCHEMA = _fields_of("na1")       # na1's, for direct na1dream callers

# Active maps (overridden per run by set_schema).
CPP_VAR, CPP_FIELDS, ARG_FIELDS = _DEFAULT_SCHEMA
PROV_FIELDS = CPP_FIELDS                  # back-compat alias (NA1's province map)

def set_schema(game):
    """Select the per-title record field maps and asset enums (called by koei_vm.decompile)."""
    global CPP_VAR, CPP_FIELDS, ARG_FIELDS, _ARG_ENUMS, _ENUMS, _EXTOP_OVERRIDE, _RECORD
    CPP_VAR, CPP_FIELDS, ARG_FIELDS = _fields_of(game) or _DEFAULT_SCHEMA
    _RECORD = _record_of(game)
    _ARG_ENUMS, _ENUMS = _load_asset_enums(game)
    _EXTOP_OVERRIDE = _EXTOP_OVERRIDES_NA1 if game == "na1" else {}


def annotate_field_access(expr, record_var="arg1", field_map=PROV_FIELDS):
    """Rewrite *(word*)((record + N)) as record->fieldname for known offsets."""
    # Match *(word*)((record_var + N))
    pat = re.compile(rf'\*\(word\*\)\(\({re.escape(record_var)} \+ (\d+)\)\)')
    def rep(m):
        off = int(m.group(1))
        name = field_map.get(off, f"field_{off}")
        return f"{record_var}->{name}"
    return pat.sub(rep, expr)


# Rewrite byte-array accesses `*(byte*)((IDX + 0xNNNN))` as `array_label[IDX]`
# when 0xNNNN is a labeled address. Restricted to byte derefs with a non-strided
# index (no `*`), so it does NOT touch the word-stride province records
# (`(arg1 * 26) + 0x7001`), which annotate_record_access renders. This is what turns
# the per-fief/per-daimyo flag arrays (ai_war_target_flags, fief_to_daimyo_map, …)
# legible across every indexed access.
# Index = a bare identifier (local9, arg1) optionally with a call's arg-parens
# (ui_helper_d772(arg1)). NOT a parenthesized arithmetic group like (arg1 * 26),
# so word-stride record bases are excluded (they're also *(word*), not *(byte*)).
_ARRAY_ACCESS_RE = re.compile(r'\*\(byte\*\)\(\((\w+(?:\([^()]*\))?) \+ 0x([0-9A-Fa-f]{4})\)\)')
# Named-base form: once a ROM/RAM table base is resolved to its label at the load
# (e.g. loadB_imm_word $F70E -> province_to_mapid_table), the deref reads
# `*(byte*)((idx + province_to_mapid_table))`. Match that too so it still collapses
# to `table[idx]` — otherwise resolving the base degrades arr[i] to pointer math.
_ARRAY_ACCESS_NAMED_RE = re.compile(r'\*\(byte\*\)\(\((\w+(?:\([^()]*\))?) \+ ([a-z_][A-Za-z0-9_]*)\)\)')

_NAME_SET_CACHE = {}
def _label_name_set(labels):
    key = id(labels)
    s = _NAME_SET_CACHE.get(key)
    if s is None:
        s = {_label_name(labels, a) for a in labels}
        s.discard(None)
        _NAME_SET_CACHE[key] = s
    return s


def annotate_array_access(text, labels):
    if not labels:
        return text
    def rep_hex(m):
        idx, addr_hex = m.group(1), m.group(2)
        name = _label_name(labels, int(addr_hex, 16))
        return f"{name}[{idx}]" if name else m.group(0)
    text = _ARRAY_ACCESS_RE.sub(rep_hex, text)
    names = _label_name_set(labels)
    def rep_named(m):
        idx, base = m.group(1), m.group(2)
        # base is the table (a known label); guard idx isn't itself a label so we
        # never rewrite an ambiguous (label_a + label_b).
        if base in names and idx not in names:
            return f"{base}[{idx}]"
        return m.group(0)
    return _ARRAY_ACCESS_NAMED_RE.sub(rep_named, text)


# Strided record tables as C arrays of structs (the title profile's [fields] type / base / stride).
# The engine prints a record access as `((E * stride) + 0xADDR)`, the field folded into ADDR; this
# renders it as &table[E].field, a word read of it as table[E].field, and the bare base as &table[E].
# A local whose one assignment in the sub is &table[E] then reads as local->field. Only the
# `(E * stride) + base..base+stride-1` shape matches, so a number such as 0x7530 is never touched,
# and an offset with no field name (a byte field) is left as it was.
def _record_of(game):
    from emu_harness.vm.koei.nes.titles import load_games
    f = load_games().get(game, {}).get("fields") or {}
    if "base" not in f:
        return None
    return {"type": f["type"], "base": f["base"], "stride": f["stride"],
            "fields": {int(k): v for k, v in f.get("province", {}).items()}}


_RECORD = _record_of("na1")
_WORD_DEREF = "*(word*)("


def _match_open(text, close):
    """Index of the '(' that the ')' at `close` closes, or -1."""
    depth = 0
    for i in range(close, -1, -1):
        if text[i] == ")":
            depth += 1
        elif text[i] == "(":
            depth -= 1
            if depth == 0:
                return i
    return -1


def annotate_record_access(text, labels, rec=None):
    rec = rec or _RECORD
    if not rec:
        return text
    base, stride, fields = rec["base"], rec["stride"], rec["fields"]
    table = _label_name(labels, base) if labels else None
    table = table or f"{rec['type']}_table"
    pat = re.compile(rf" \* {stride}\) \+ 0x([0-9A-Fa-f]{{4}})\)")
    pos = 0
    while True:
        m = pat.search(text, pos)
        if not m:
            return text
        off = int(m.group(1), 16) - base
        inner = _match_open(text, m.start() + len(f" * {stride}"))      # the ( before E
        if not 0 <= off < stride or inner < 1 or text[inner - 1] != "(":
            pos = m.end(); continue
        start, end, idx = inner - 1, m.end(), text[inner + 1:m.start()]
        tail = re.match(r" \+ (\d+)\)", text[end:])                  # (((E * s) + 0xB) + N)
        if tail and start >= 1 and text[start - 1] == "(" and 0 <= off + int(tail.group(1)) < stride:
            start, end, off = start - 1, end + tail.end(), off + int(tail.group(1))
        if off and off not in fields:
            pos = m.end(); continue
        elem = f"{table}[{idx}]"
        new = f"&{elem}.{fields[off]}" if off else f"&{elem}"
        lead = start - len(_WORD_DEREF)
        if lead >= 0 and text[lead:start] == _WORD_DEREF and text[end:end + 1] == ")" and off in fields:
            start, end, new = lead, end + 1, f"{elem}.{fields[off]}"
        text = text[:start] + new + text[end:]
        pos = start + len(new)


def annotate_record_locals(lines, rec=None):
    """[(addr, indent, text)] -> the same, with a record-pointer local's accesses as local->field."""
    rec = rec or _RECORD
    if not rec:
        return lines
    fields = rec["fields"]
    assigns = {}
    for _a, _i, t in lines:
        for m in re.finditer(r"\b(local\d+) = ", t):
            assigns.setdefault(m.group(1), []).append(t)
    ptrs = [v for v, ts in assigns.items()
            if len(ts) == 1 and re.search(rf"\b{v} = &\w+\[[^;]*\];", ts[0]) and not re.search(rf"\b{v} = &\w+\[[^;]*\]\.", ts[0])]
    if not ptrs:
        return lines
    out = []
    for a, i, t in lines:
        for v in ptrs:
            t = re.sub(rf"\*\(word\*\)\(\({v} \+ (\d+)\)\)",
                       lambda m: f"{v}->{fields[int(m.group(1))]}" if int(m.group(1)) in fields else m.group(0), t)
            t = re.sub(rf"\*\(word\*\)\({v}\)", f"{v}->{fields[0]}", t) if 0 in fields else t
            t = re.sub(rf"\({v} \+ (\d+)\)",
                       lambda m: f"&{v}->{fields[int(m.group(1))]}" if int(m.group(1)) in fields else m.group(0), t)
        out.append((a, i, t))
    return out


def record_declarations(labels, rec=None):
    """The C declaring the record table the annotations use, or "" (prepended to a bank's C)."""
    rec = rec or _RECORD
    if not rec:
        return ""
    table = (_label_name(labels, rec["base"]) if labels else None) or f"{rec['type']}_table"
    members = " ".join(f"word {n};" for _o, n in sorted(rec["fields"].items()))
    return (f"struct {rec['type']} {{ {members} }};    /* {rec['stride']} bytes */\n"
            f"extern struct {rec['type']} {table}[];    /* at ${rec['base']:04X} */\n\n")


# Labelled RAM arrays indexed by arithmetic, rendered by array name. A bank-level pass over the whole
# bank's C (titles.decompile), because each array's C declaration must agree with every use in the file:
#   ((E * S) + 0xB), S > 2   -> NAME[E]       declared byte NAME[][S]   (a row; e.g. a 9-byte name)
#   ((E << 1) + 0xB)         -> &NAME[E]      declared word NAME[]      (a word read of it -> NAME[E])
#   (E + 0xB)                -> &NAME[E]      declared byte NAME[]      (a byte read of it -> NAME[E])
# B must be a labelled RAM address and E must not be a literal, so a number is never renamed (a bare
# 0x6F4F with no index is left alone: there an address and a number look the same). An array used in
# two shapes in one file renders only its commoner shape, so no declaration contradicts a use. The
# record table (_RECORD) is excluded: annotate_record_access owns it.
_RAM_LO, _RAM_HI = 0x6000, 0x8000


def _ram_array_sites(text, labels):
    """[(start, end, name, shape, stride, index, deref)] for every array-shaped site, innermost first."""
    sites = []
    rec = _RECORD
    for m in re.finditer(r" \+ 0x([67][0-9A-Fa-f]{3})\)", text):
        b = int(m.group(1), 16)
        if not _RAM_LO <= b < _RAM_HI or (rec and rec["base"] <= b < rec["base"] + rec["stride"]):
            continue
        name = _label_name(labels, b)
        if not name:
            continue
        end = m.end()
        start = _match_open(text, end - 1)
        if start < 0:
            continue
        x = text[start + 1:m.start()]                            # the index expression E (or (E*S), (E<<1))
        shape, stride, idx = "byte", 1, x
        g = re.fullmatch(r"\((.+) \* (\d+)\)", x)
        if g and _match_open(x, len(x) - 1) == 0 and int(g.group(2)) > 2:
            shape, stride, idx = "row", int(g.group(2)), g.group(1)
        g = re.fullmatch(r"\((.+) << 1\)", x)
        if g and _match_open(x, len(x) - 1) == 0:
            shape, stride, idx = "word", 2, g.group(1)
        if re.fullmatch(r"-?(0x[0-9A-Fa-f]+|\d+)", idx.strip()):
            continue
        deref = None
        for cast in ("*(word*)(", "*(byte*)("):
            if text[max(0, start - len(cast)):start] == cast and text[end:end + 1] == ")":
                deref = cast
        sites.append((start, end, name, shape, stride, idx, deref))
    return sites


def render_ram_arrays(text, labels):
    """A bank's C -> (C with array-shaped RAM accesses by name, the declarations it needs)."""
    if not labels:
        return text, ""
    uses = {}
    for *_s, name, shape, stride, _i, _d in _ram_array_sites(text, labels):
        uses.setdefault(name, {}).setdefault((shape, stride), 0)
        uses[name][(shape, stride)] += 1
    chosen = {n: max(c.items(), key=lambda kv: kv[1])[0] for n, c in uses.items()}
    done = set()
    while True:
        sites = [s for s in _ram_array_sites(text, labels) if chosen.get(s[2]) == (s[3], s[4])]
        if not sites:
            break
        start, end, name, shape, stride, idx, deref = max(sites, key=lambda s: s[0])   # rightmost first
        if shape == "row":
            new = f"{name}[{idx}]"
        elif (shape, deref) in (("word", "*(word*)("), ("byte", "*(byte*)(")):
            start, end, new = start - len(deref), end + 1, f"{name}[{idx}]"
        else:
            new = f"&{name}[{idx}]"
        text = text[:start] + new + text[end:]
        done.add(name)
    decl = "".join(f"extern byte {n}[][{s}];\n" if sh == "row" else f"extern {sh} {n}[];\n"
                   for n, (sh, s) in sorted(chosen.items()) if n in done)
    return text, decl + ("\n" if decl else "")


# --- Spec-independent dispatch: normalize the mnemonic from the OPCODE BYTE ---
# The decompile dispatch below keys on the original ("old-spec") mnemonics. Rather
# than couple to ANY mnemonic spelling, we derive the canonical name from the
# opcode byte (bytes[0]) — invariant across spec revisions. This lets the
# decompiler consume disasm produced with EITHER the old spec or the new
# vm-disasm.py (v2 OPCODE_INFO names like LOADL_quick / CALL_abs / RETURN), since
# the opcode byte is the same regardless of how the listing labels it.
def _build_opcode_to_mnemonic():
    m = {}
    rng = lambda a, b, name: m.update({op: name for op in range(a, b + 1)})
    rng(0x00, 0x0B, 'loadA_local_neg');  rng(0x0C, 0x0F, 'loadA_local_pos')
    rng(0x10, 0x1B, 'loadB_local_neg');  rng(0x1C, 0x1F, 'loadB_local_pos')
    rng(0x20, 0x2B, 'storeA_local_neg'); rng(0x2C, 0x2F, 'storeA_local_pos')
    # $30-$3F are PUSH_quick (per OPCODE_INFO, the semantics authority), NOT storeB.
    # They push a frame slot's VALUE onto the data stack (locals for $30-$3B, args for
    # $3C-$3F = arg-forwarding to a call). The old "storeB_local" mapping (store regB ->
    # slot) was the buggy legacy mnemonic and is the root of a large share of the bogus
    # `argN = ?` / `/*stack underflow*/` markers. Oracle-confirmed (forwarder stubs +
    # $38 push local8). See [[project_nobunaga_vm_decompiler]] / ROADMAP frame-arg fix.
    rng(0x30, 0x3B, 'push_local_neg'); rng(0x3C, 0x3F, 'push_local_pos')
    rng(0x40, 0x4F, 'loadA_imm4')   # LOADL_qimm: regL = opcode & 0x0F (0..15), NOT a clear
    m[0x50] = 'setB_imm4_50';            rng(0x51, 0x5F, 'setB_imm4')
    rng(0x60, 0x6F, 'push_imm4');        m[0x70] = 'nop'
    rng(0x71, 0x7F, 'addA_imm4')
    m[0x80] = 'trigger_syscall_80'
    m[0x81] = 'op_81_byte'; m[0x82] = 'op_82_bb'; m[0x83] = 'op_83_byte'
    m[0x84] = 'op_84_bwb'; m[0x85] = 'op_85_byte'; m[0x86] = 'op_86_bwb'
    m[0x87] = 'op_87_byte'; m[0x88] = 'op_88_bwb'
    m[0x89] = 'loadA_imm_sbyte'; m[0x8A] = 'loadA_imm_word'
    m[0x8B] = 'loadB_imm_byte';  m[0x8C] = 'loadB_imm_word'
    m[0x8D] = 'op_8D_sbyte'
    m[0x8E] = 'push_imm_word';   m[0x8F] = 'addA_imm_sbyte'
    m[0x90] = 'op_90_bb'
    rng(0x91, 0x9F, 'trigger_syscall_9X')
    rng(0xA0, 0xA3, 'op_A0_A3_byte')
    # $A4-$AB absolute-memory family — AUDITED 2026-06-01 (data-walk B2 fallout).
    # Direction can't be read from the handler bytes (the $EFD5/$EFA6/$EFEC prelude sets
    # up whether $00 points at the operand-address or the register file, so $EA75 etc.
    # look like loads either way) — the OPCODE USAGE PATTERN is the only reliable arbiter.
    #   $A8: PROVEN store by the year++ (`$A48E LOADL_abs $6D9F / INC / $A492 STORE_abs`);
    #        was wrongly aliased to loadA_mem_word -> 267 dropped stores. Fixed below.
    #   $A7: 0 sites in the whole ROM (verified) — the loadA_addr_word mapping is DEAD
    #        code, harmless. NOT changed to the spec's BYTE_LOADR_abs: unused == unvalidatable.
    #   $AA (266 sites, renders via PUSH_abs) / $AB (5) / op_AA_wb: 0 // TODO residue, render OK.
    m[0xA4] = 'loadA_mem_word';  m[0xA5] = 'loadA_mem_byte'
    m[0xA6] = 'loadB_mem_word';  m[0xA7] = 'loadA_addr_word'   # $A7: 0 sites (dead)
    m[0xA8] = 'storeA_mem_word'; m[0xA9] = 'storeA_mem_byte'
    m[0xAB] = 'op_AB_word'; m[0xAD] = 'op_AD_byte'
    m[0xAC] = 'host_call_simple'; m[0xAE] = 'adjust_stack_sbyte'
    m[0xB0] = 'loadA_ind_word';  m[0xB1] = 'storeA_ind_word'
    m[0xB2] = 'nop_B2';          m[0xB3] = 'pushA';     m[0xB4] = 'popB'
    m[0xB5] = 'mul';   m[0xB6] = 'div_signed';   m[0xB7] = 'ext_op'
    m[0xB8] = 'div_unsigned'
    m[0xB9] = 'mod_signed'; m[0xBA] = 'mod_unsigned'
    m[0xBB] = 'add';   m[0xBC] = 'sub'
    m[0xBD] = 'shl_by_regB'; m[0xBE] = 'lshr_by_regB'; m[0xBF] = 'ashr_by_regB'
    # Compare block — ORACLE-CONFIRMED vs the live ROM handler (probe-compare-opcodes.py)
    # and matches vm-opcodes-v2.toml (SCMP*/UCMP*). Regular layout: signed [lt,le,gt,ge]
    # = C2-C5, unsigned [lt,le,gt,ge] = C6-C9. (Earlier hand map had C3/C5 le<->ge swapped
    # and C6/C9 lt<->ge swapped, rendering those four comparisons BACKWARDS everywhere.)
    m[0xC0] = 'cmp_eq'; m[0xC1] = 'cmp_ne'; m[0xC2] = 'cmp_slt'; m[0xC3] = 'cmp_sle'
    m[0xC4] = 'cmp_sgt'; m[0xC5] = 'cmp_sge'; m[0xC6] = 'cmp_ult'; m[0xC7] = 'cmp_ule'
    m[0xC8] = 'cmp_ugt'; m[0xC9] = 'cmp_uge'; m[0xCA] = 'is_zero'
    m[0xCB] = 'op_CB';   m[0xCD] = 'swap_AB'; m[0xCE] = 'trigger_syscall_CE'
    m[0xCF] = 'vm_return'
    m[0xD0] = 'incA'; m[0xD1] = 'decA'; m[0xD2] = 'aslA'
    m[0xD3] = 'loadA_ind_byte'; m[0xD4] = 'storeA_ind_byte'; m[0xD5] = 'switch'
    m[0xD6] = 'jump_abs'; m[0xD7] = 'branch_nz_abs'; m[0xD8] = 'branch_z_abs'
    m[0xD9] = 'switch'   # SWITCH_noncontig (vm-disasm decodes the table into the comment)
    m[0xDA] = 'bitand'; m[0xDB] = 'bitor'; m[0xDC] = 'bitxor'
    m[0xDD] = 'host_call_indirect_simple'
    m[0xDE] = 'loadA_frameaddr'; m[0xDF] = 'loadB_frameaddr'
    m[0xE0] = 'op_E0_byte'; m[0xE1] = 'op_E1_byte'; m[0xE2] = 'op_E2_byte'
    m[0xE4] = 'branch_nz_rel'; m[0xE5] = 'branch_z_rel'; m[0xE6] = 'jump_rel_fwd'
    m[0xE7] = 'branch_nz_rel_fwd'; m[0xE8] = 'branch_z_rel_fwd'
    m[0xE9] = 'host_call'; m[0xEA] = 'host_call_indirect'
    for op in (0xEC, 0xED, 0xEE, 0xEF, 0xF2, 0xF3, 0xF4, 0xF6, 0xF7, 0xF9, 0xFA, 0xFB, 0xFE):
        m[op] = 'trigger_syscall_EB_FE'
    m[0xFF] = 'op_FF'
    return m

OPCODE_TO_MNEMONIC = _build_opcode_to_mnemonic()


# ext_op_table ($F263) — VM opcode $B7 ('ext_op'). index -> handler CPU addr.
# Decoded from this ROM via ext_op_dispatch ($F246): the dispatcher does asl/tax then
# LDA $F261,X, so handler(N) = little-endian word at $F261 + 2*N. Hence N=$01 = umul32,
# N=$00 = $0000 (the unused jmp-operand slot). Cross-verified against the table bytes +
# dispatcher addressing 2026-05-31 (corrects the ROADMAP's off-by-one map). Names resolve
# through mesen-labels.toml [prg.bank15] (Session A walk).
EXT_OP_TABLE = {
    0x00: 0x0000, 0x01: 0xF2C1, 0x02: 0xF2F0, 0x03: 0xF37F, 0x04: 0xF38C,
    0x05: 0xF427, 0x06: 0xF446, 0x07: 0xF457, 0x08: 0xF467, 0x09: 0xF495,
    0x0A: 0xF47E, 0x0B: 0xF4AC, 0x0C: 0xF54D, 0x0D: 0xF55B, 0x0E: 0xF569,
    0x0F: 0xF577, 0x10: 0xF57C, 0x11: 0xF581, 0x12: 0xF586, 0x13: 0xF58B,
    0x14: 0xF5A3, 0x15: 0xF5AC, 0x16: 0xF5C2, 0x17: 0xF531, 0x18: 0xF4F4,
    0x19: 0xF500, 0x1A: 0xF53F, 0x1B: 0xF51A, 0x1C: 0xF515, 0x1D: 0xF43B,
    0x1E: 0xF36B, 0x1F: 0xF399, 0x20: 0xF3B5, 0x21: 0xF3F7, 0x22: 0xF403,
    0x23: 0xF40F, 0x24: 0xF41B, 0x25: 0xF4E9, 0x26: 0xF4EF, 0x27: 0xF4F3,
    0x28: 0xF433, 0x29: 0xF475, 0x2A: 0xF4A3, 0x2B: 0xF48C, 0x2C: 0xF4BA,
    0x2D: 0xF3DB, 0x2E: 0xF2FB, 0x2F: 0xF362,
}


# Kernel syscall table — VM calls $F226 (syscall_dispatch) via CALL_abs_imm1; the
# 1-byte immediate IS the syscall ID, pushed LAST (right before the call), so under
# the reverse-push display convention (M3, 2026-06-03) it renders as the FIRST
# argument of the emitted `syscall_dispatch(...)` expression. Resolve it to the
# handler name (the
# 23-entry surface catalogued in 04-syscall-api.md). IDs 2/11/14/15 are RTS
# placeholder slots — deliberately ABSENT so they stay `syscall_dispatch(…, N)`
# (honest "unused slot" signal) rather than getting a fake name.
SYSCALL_TABLE = {
    0:  "syscall_reset",
    1:  "syscall_ppu_upload_block",
    3:  "syscall_set_sprite",
    4:  "syscall_palette_write",
    5:  "syscall_fill_nametable",
    6:  "syscall_read_controller",
    7:  "syscall_call_bank",
    8:  "syscall_fill_attr_quadrant",
    9:  "syscall_audio_load_voice",
    10: "syscall_audio_control",
    12: "syscall_ppu_fill_rect",
    13: "syscall_ppu_render_rect",
    16: "syscall_memcpy_banked",
    17: "syscall_rng_next",
    18: "syscall_palette_swap",
    19: "syscall_wait_for_nmi",
    20: "syscall_ppu_copy_rect",
    21: "syscall_set_chr_bank0_reg",
    22: "syscall_sram_block_with_checksum",
}


def _as_int(tok):
    """Parse a C integer literal token ('6', '0x16', '22') -> int, else None."""
    tok = tok.strip()
    try:
        return int(tok, 16) if tok.lower().startswith('0x') else int(tok, 10)
    except ValueError:
        return None


# (bank, addr) -> name, populated per-title by koei_vm from every [prg.bankN] section.
# Lets annotate_bank_call resolve the cross-bank trampoline to the real target name.
BANK_CALL_MAP = {}

# The decompiled NAME of the cross-bank call trampoline, per title (koei_vm sets it from games.toml
# `bankcall_fn`). NA1/NA2 = "call_in_bank_dcd1"; rot3k2 = "call_in_bank" ($F412). Must match the label
# the trampoline address carries in that title's toml. The trailing "(" is appended by the caller.
BANKCALL_NEEDLE = "call_in_bank_dcd1("


def _split_top_level(s):
    """Split a comma-separated arg list on TOP-LEVEL commas only (respects nested
    parens, so `syscall_dispatch(a, foo(x, y), 6)` splits into 3, not 4)."""
    args, depth, start = [], 0, 0
    for i, c in enumerate(s):
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        elif c == ',' and depth == 0:
            args.append(s[start:i])
            start = i + 1
    args.append(s[start:])
    return args


def annotate_syscall(text):
    """Rewrite `syscall_dispatch(ID, arg…)` -> `syscall_<name>(arg…)`.

    The LEADING argument is the kernel syscall ID (the CALL_abs_imm1 immediate to
    $F226). It's the last-pushed value, so the reverse-push display convention (M3)
    puts it first. Resolve it through SYSCALL_TABLE and drop it from the visible arg
    list, leaving the handler's actual parameters. Handles nested syscalls and
    balanced parens; leaves unknown/placeholder IDs (and non-literal leading args)
    untouched.
    """
    needle = "syscall_dispatch("
    out, i = [], 0
    while True:
        j = text.find(needle, i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        # Walk to the matching close paren.
        k, depth = j + len(needle), 1
        start = k
        while k < len(text) and depth:
            depth += (text[k] == '(') - (text[k] == ')')
            k += 1
        if depth != 0:                      # unbalanced — bail, emit verbatim
            out.append(text[j:])
            break
        args = _split_top_level(text[start:k - 1])
        id_val = _as_int(args[0]) if args else None
        name = SYSCALL_TABLE.get(id_val)
        if name:
            rest = ", ".join(a.strip() for a in args[1:])
            out.append(f"{name}({rest})")
        else:
            out.append(text[j:k])           # keep raw dispatch for unmapped IDs
        i = k
    return "".join(out)


# Parse a line of the disasm output. Returns (addr, opcode_byte, mnemonic, inline_op, comment)
LINE_RE = re.compile(
    r'^\s*[>]?\$([0-9A-Fa-f]{4})\s+([0-9A-Fa-f]{2}(?:\s+[0-9A-Fa-f]{2})*)\s+(\S+)(.*)$'
)


def parse_listing(filepath, sub_addr):
    """Extract the lines of one sub from a bank_NN_vm.asm file."""
    sub_header = f"; sub ${sub_addr:04X}"
    in_sub = False
    body_addr = None
    instructions = []  # (addr, bytes, mnemonic, operand_repr, comment)
    for line in Path(filepath).read_text(encoding='utf-8', errors='ignore').splitlines():
        if line.startswith(';'):
            if sub_header in line:
                in_sub = True
                # Parse "body @ $XXXX"
                m = re.search(r'body @ \$([0-9A-Fa-f]{4})', line)
                if m:
                    body_addr = int(m.group(1), 16)
                continue
            if in_sub and "; sub $" in line:
                # next sub — done
                break
            continue
        if not in_sub:
            continue
        if "; ===" in line:
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        addr = int(m.group(1), 16)
        bytes_hex = m.group(2).split()
        mnemonic = m.group(3)
        rest = m.group(4).strip()
        # Parse operand
        operand_repr = None
        # Operand is everything before the ";" comment marker
        if ';' in rest:
            operand_repr, comment = rest.split(';', 1)
            operand_repr = operand_repr.strip()
            comment = comment.strip()
        else:
            operand_repr = rest.strip()
            comment = ""
        raw_bytes = [int(b, 16) for b in bytes_hex]
        # Normalize the mnemonic from the opcode byte — spec-independent, so this
        # works whether the listing used old-spec names or vm-disasm.py's v2 names.
        canonical = OPCODE_TO_MNEMONIC.get(raw_bytes[0], mnemonic)
        instructions.append({
            'addr': addr,
            'bytes': raw_bytes,
            'mnemonic': canonical,
            'operand': operand_repr,
            'comment': comment,
        })
    return body_addr, instructions


def annotate_bank_call(text):
    """Rewrite `call_in_bank_dcd1(window, bankval, addr, args…)` -> `<target>(args…)`.

    The compiler's cross-bank call is a trampoline through $DCD1: window (the PRG slot),
    bankval (target bank = bankval & 31), addr (entry point in that bank), then the real
    args. Resolve (bank, addr) through BANK_CALL_MAP and drop the three trampoline args,
    leaving the call as if it were direct. Targets whose bank isn't named yet are left raw.
    """
    needle = BANKCALL_NEEDLE
    out, i = [], 0
    while True:
        j = text.find(needle, i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        k, depth = j + len(needle), 1
        start = k
        while k < len(text) and depth:
            depth += (text[k] == '(') - (text[k] == ')')
            k += 1
        if depth != 0:                      # unbalanced — bail, emit verbatim
            out.append(text[j:])
            break
        args = _split_top_level(text[start:k - 1])
        bv = _as_int(args[1]) if len(args) > 1 else None
        addr = _as_int(args[2]) if len(args) > 2 else None
        name = BANK_CALL_MAP.get((bv & 31, addr)) if (bv is not None and addr is not None) else None
        if name:
            rest = ", ".join(a.strip() for a in args[3:])
            out.append(f"{name}({rest})")
        else:
            out.append(text[j:k])           # target bank not named yet — keep raw
        i = k
    return "".join(out)


def _mem_name(operand, addr, labels):
    """Name an absolute-memory operand. Precedence: the disasm's own inline
    `(label)` -> the toml labels dict (covers [ram]/SRAM and data addresses, since
    load_labels() reads every "0xNNNN" line regardless of section) -> mem_XXXX.
    This is the variable-side twin of function naming: a name added to the toml's
    [ram] block surfaces here on the next decompile-all, no asm regen required."""
    label_m = re.search(r'\(([a-z_][a-z0-9_]*)\)', operand)
    if label_m:
        return label_m.group(1)
    return _label_name(labels, addr) or f"mem_{addr:04X}"


def _label_name(labels, addr):
    """Look up a label name for `addr`. Accepts either addr->name (str) or
    addr->(name, comment) (the nobunaga_vm.labels shape). Returns None if absent.
    Sanitizes to a valid C identifier."""
    if not labels:
        return None
    v = labels.get(addr)
    if v is None:
        return None
    name = v[0] if isinstance(v, tuple) else v
    name = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if name and name[0].isdigit():
        name = "_" + name
    return name or None


def _imm_word_repr(val, labels):
    """Render a 16-bit immediate. ROM pointers ($8000+) resolve to their label
    (string/table/code-pointer name) — this is the B1 ROM-data surface. RAM bases
    ($6xxx/$7xxx) are deliberately LEFT as hex so the downstream annotate_array_access
    pass can still turn `(idx + 0x6CF7)` into `province_ai_state[idx]`; resolving them
    here would pre-empt that and degrade `arr[i]` to raw pointer math. `labels` is the
    per-bank dict, so a $BXXX string resolves to the right bank's name."""
    if val >= 0x8000:
        nm = _label_name(labels, val)
        if nm:
            return nm
    return str(val) if val < 256 else f"0x{val:04X}"


# The four quick-opcode families that address a frame ARG slot (vs a local): the
# low nibble 0x0C-0x0F selects arg1..arg4 (fp+0x0B/0D/0F/11). See local_name('pos').
_POS_ARG_MNEMONICS = frozenset({
    'loadA_local_pos', 'loadB_local_pos', 'storeA_local_pos', 'push_local_pos',
})


# General signed-offset frame opcodes that render a slot BY NAME (frame_word/_byte/_addr
# resolve via _slot_name) — so an arg reached only through one of these is still named
# argN and must be counted. Includes the byte ops ($A0-$A3): they now name the slot too
# (`*(byte*)&argN`), so a byte-only arg would otherwise leave the signature under-declared.
_GEN_FRAME_MNEMONICS = frozenset({
    'op_81_byte', 'op_82_bb', 'op_83_byte', 'op_84_bwb',
    'op_85_byte', 'op_86_bwb', 'op_87_byte', 'op_88_bwb',
    'loadA_frameaddr', 'loadB_frameaddr', 'op_A0_A3_byte',
})


def _body_arg_count(instructions):
    """Highest arg slot the body references, i.e. the real parameter count (0 -> `void`).
    Counts every access path that NAMES an arg (so the count == what renders as argN):
    the quick arg-opcodes (low nibble 0x0C-0x0F -> arg1..arg4) AND the general
    signed-offset frame opcodes (word $81-$88/$DE/$DF + byte $A0-$A3) landing on a
    positive odd offset >= +0x0B (arg5+ live ONLY here -- the quick opcodes top out at
    arg4; subs reach ~6 args)."""
    n = 0
    for ins in instructions:
        mn = ins['mnemonic']
        if mn in _POS_ARG_MNEMONICS:
            low = ins['bytes'][0] & 0x0F
            if low >= 0x0C:                       # 0x0C..0x0F -> arg1..arg4
                n = max(n, low - 0x0B)
        elif mn in _GEN_FRAME_MNEMONICS:
            off = _signed_operand(ins['operand'])
            if off >= 0x0B and off % 2 == 1:      # +0x0B,+0x0D,... -> arg1,arg2,...
                n = max(n, (off - 0x0B) // 2 + 1)
    return n


# --- var-walk: per-(sub, slot) semantic names for args/locals (M4, 2026-06-03) ---
# The variable-side twin of the label table. The decompiler names frame slots
# positionally (arg1..arg4, local0..local11); var-walk replaces those with role
# names (fief, amount, gain, …) recovered by usage-role inference + caller
# propagation. Storage lives in mesen-labels.toml as `[vars.bankN."0xADDR"]`
# sections (ADDR = the sub's stub address), one `slot = { name = "...", comment }`
# line per renamed slot. Regex-parsed (no tomllib dep), exactly like load_labels;
# the slot lines don't match the `"0x..."=` label regex so the label loaders ignore
# them, and the section header resets the `[prg.bankN]` collectors (which bail on any
# non-prg `[`), so the two tables never interfere.
def load_var_names(toml_path):
    """Parse `[vars.bankN."0xADDR"]` sections -> {(bank, addr): {slot: name}}.
    Slot keys are positional tokens (`arg1`/`localN`, bare) OR far-frame displacement
    keys (`"fp-38"`/`"fp+40"`, quoted) — the latter for spill locals the disassembler
    renders as raw `*(word*)(fp - 38)` rather than one of the 12 standard slots."""
    text = Path(toml_path).read_text(encoding="utf-8")
    sec_re = re.compile(r'^\s*\[vars\.bank(\d+)\."0x([0-9A-Fa-f]+)"\]')
    slot_re = re.compile(r'^\s*(?:([A-Za-z_]\w*)|"(fp[+-]\d+)")\s*=\s*\{\s*name\s*=\s*"([^"]+)"')
    out, cur = {}, None
    for line in text.splitlines():
        m = sec_re.match(line)
        if m:
            cur = (int(m.group(1)), int(m.group(2), 16))
            out.setdefault(cur, {})
            continue
        if line.lstrip().startswith('['):       # left the vars sections
            cur = None
            continue
        if cur is not None:
            sm = slot_re.match(line)
            if sm:
                out[cur][sm.group(1) or sm.group(2)] = sm.group(3)
    return out


# Far-frame slot key -> the exact displacement string vm-disasm renders (`_fp_disp`).
_FAR_KEY_RE = re.compile(r'fp([+-])(\d+)$')


def _far_disp(key):
    """`'fp-38'` -> `'fp - 38'` (the literal `_fp_disp` form), or None if not a far key."""
    m = _FAR_KEY_RE.match(key)
    return f"fp {m.group(1)} {int(m.group(2))}" if m else None


def apply_var_names(text, vmap):
    """Substitute frame-slot placeholders with their semantic names. Run LAST in the
    emit pipeline — after annotate_field_access et al., which key on the literal
    arg1..arg4, so `arg1->output` is already formed and just gets its base renamed.

    Two slot classes, disjoint text:
      • Positional tokens (`arg1`/`localN`): word-bounded single-pass alternation
        (no cascade — `local1` never eats `local11`).
      • Far-frame slots (`fp-38`): replace the WHOLE rendered lvalue/rvalue. Every
        `_fp_disp` form ends in `)`, so the displacement is closing-paren-anchored
        (no `fp - 3` vs `fp - 38` prefix collision). Deref forms for ALL keys go
        first, THEN the bare address-of form — so the `(fp - 38)` inside
        `*(word*)(fp - 38)` isn't eaten before its deref match."""
    if not vmap:
        return text
    pos = {k: v for k, v in vmap.items() if not k.startswith("fp")}
    far = {d: vmap[k] for k in vmap if (d := _far_disp(k))}
    if pos:
        keys = sorted(pos, key=len, reverse=True)
        pat = re.compile(r'\b(' + '|'.join(re.escape(k) for k in keys) + r')\b')
        text = pat.sub(lambda m: pos[m.group(1)], text)
    for disp, name in far.items():                    # 1) typed derefs (word + byte)
        text = text.replace(f"*(word*)({disp})", name).replace(f"*(byte*)({disp})", name)
    for disp, name in far.items():                    # 2) bare address-of, after derefs gone
        text = text.replace(f"({disp})", f"&{name}")
    return text


# --- Emission families: data-driven dispatch for the mechanical opcodes (A, teardown) ---
# The ~370-line dispatch in decompile() was a flat if/elif on mnemonics where ~50 arms
# were near-identical "regA = <formula of regA/regB>" lines. Those collapse to these
# tables + one generic block each, so the whole arithmetic/compare/logic family reads at
# a glance and a new op is a one-line table entry (portable to another Sea-16 title by
# swapping the table, not editing control flow). The genuinely-distinct opcodes
# (host_call/switch/ext_op/frame access/branches/calls) stay as explicit handlers below.
#
# _BINOP: `regA = (regA <op> regB)`. cast = how operands are signedness-cast:
#   'none' signed, 'both' both `(unsigned)`, 'lhs' only the left (logical right-shift).
_BINOP = {
    # signed / no cast
    'add': ('+', 'none'), 'sub': ('-', 'none'), 'mul': ('*', 'none'),
    'div_signed': ('/', 'none'), 'mod_signed': ('%', 'none'),
    'cmp_slt': ('<', 'none'), 'cmp_sgt': ('>', 'none'), 'cmp_sle': ('<=', 'none'),
    'cmp_sge': ('>=', 'none'), 'cmp_eq': ('==', 'none'), 'cmp_ne': ('!=', 'none'),
    'bitand': ('&', 'none'), 'bitor': ('|', 'none'), 'bitxor': ('^', 'none'),
    'shl_by_regB': ('<<', 'none'), 'shr_by_regB': ('>>', 'none'), 'ashr_by_regB': ('>>', 'none'),
    # unsigned (both operands cast)
    'div': ('/', 'both'), 'div_unsigned': ('/', 'both'), 'mod_unsigned': ('%', 'both'),
    'cmp_uge': ('>=', 'both'), 'cmp_ule': ('<=', 'both'),
    'cmp_ugt': ('>', 'both'), 'cmp_ult': ('<', 'both'),
    # logical right shift: only the left operand is cast unsigned
    'lshr_by_regB': ('>>', 'lhs'),
}

# _UNARY_A: `regA = <template>` where {A} is the current regA expression. One generic
# block replaces the inc/dec/shift-by-1/zero-test/negate/deref arms.
_UNARY_A = {
    'incA': '({A} + 1)', 'decA': '({A} - 1)',
    'aslA': '({A} << 1)', 'lsrA': '({A} >> 1)',
    'is_zero': '({A} == 0)', 'op_CB': '(-{A})', 'COMPL': '(~{A})',   # $CC bitwise complement
    'loadA_ind_byte': '*(byte*)({A})', 'loadA_ind_word': '*(word*)({A})',
}


def _ternary_expr(cond, vt, vf, polarity):
    """Build `cond ? true_value : false_value` for a value-diamond. The TRUE-path value is
    the fall-through arm for a branch_z (JUMPF jumps when cond is FALSE, so fall = true),
    and the taken arm for a branch_nz. vt = fall-arm value, vf = taken-arm value."""
    tval, fval = (vt, vf) if polarity == 'z' else (vf, vt)
    return f"({cond} ? {tval} : {fval})"


def _arm_ternary(cond, vt, vf, polarity):
    """A value-diamond captures BOTH regA and regB at the arm boundaries; this builds the
    ternary for ONE register, collapsing to the bare value when the arms agree (a regA-only
    diamond carries regB in identically; the $AF46 regB-diamond carries regA in identically)."""
    return vf if vt == vf else _ternary_expr(cond, vt, vf, polarity)


def _bool_ternary_expr(formula, tval, fval):
    """`(formula ? tval : fval)` for a short-circuit boolean region, with the boolean-predicate
    simplifications `(B ? 1 : 0)` -> `B` and `(B ? 0 : 1)` -> `!(B)` (so `is_tile_in_bounds`
    reads `(x<=10 && y<=4)` not `((x<=10 && y<=4) ? 1 : 0)`)."""
    if tval == '1' and fval == '0':
        return formula
    if tval == '0' and fval == '1':
        f = formula.strip()
        return f[2:-1] if f.startswith('!(') and f.endswith(')') else f"!({formula})"
    return f"({formula} ? {tval} : {fval})"


def inline_trivial_temps(text):
    """Readability post-pass (emit-time): inline a synthetic temp (phi_*/dsel_*) that is assigned
    by a PURE expression (no call) and used EXACTLY ONCE on the immediately-following line, then drop
    the assignment. CFG- AND value-preserving — single static use, adjacent, no side effect moved.
    CONSERVATIVE BY DESIGN: a MULTI-use temp (e.g. phi_ret read by an `if` AND a `return`) or a
    call-RHS temp is left untouched; the larger per-arm/liveness class is deferred (tech-debt §4.2 —
    it is NOT gate-protected, so it needs the value-oracle, not this trivial textual pass). ~6 subs
    corpus-wide (mostly `dsel_*` selectors collapsing to clean ternaries)."""
    lines = text.split("\n")
    count = {}
    for nm in re.findall(r'\b(?:phi_\w+|dsel_\w+)\b', text):
        count[nm] = count.get(nm, 0) + 1
    out, i, n = [], 0, len(lines)
    while i < n:
        m = re.match(r'^\s*(phi_\w+|dsel_\w+)\s*=\s*(.+?);\s*(?://.*)?$', lines[i])
        if m and i + 1 < n:
            t, rhs = m.group(1), m.group(2).strip()
            nxt = lines[i + 1]
            if (count.get(t) == 2                                   # 1 assign + 1 read, whole sub
                    and re.search(r'[a-z_]\w*\(', rhs) is None       # PURE: no function call in RHS
                    and len(re.findall(r'\b' + re.escape(t) + r'\b', nxt)) == 1  # the use is next
                    and not re.match(r'^\s*' + re.escape(t) + r'\s*=', nxt)):  # ...a READ, not a reassign
                repl = rhs if (re.fullmatch(r'\w+', rhs) or re.fullmatch(r'\(.*\)', rhs)) else f'({rhs})'
                lines[i + 1] = re.sub(r'\b' + re.escape(t) + r'\b', lambda _m: repl, nxt, count=1)
                i += 1                                               # drop the assignment line
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _rebind_self_store(state, dest):
    """Emit-time fix for the "name-stale after a self-modifying store" class.

    The VM leaves the stored value live in regA (a STORE does not pop the accumulator --
    that's how `... INC; STORE x; DEC; DEREF` derefs the OLD pointer). So after a store
    `x = EXPR` where EXPR still names `x`, the variable `x` now HOLDS that value; rebind
    regA to the bare name `x` so subsequent ops fold the post-store name instead of the
    stale pre-store expression. Without it, `x = x+1` followed by a live-regA use renders
    off by the self-store delta: `*(((x+1)-1))` (post-increment `*p++`) or the double
    store `y = (x+1)` -- both read as `+N` too high when taken as sequential C.

    `dest` is usually a plain C identifier (local_name / _mem_name) -> word-boundary match
    (won't confuse local1 with local11). For an indirect self-store it is a deref
    expression like `*(word*)(arg1)` (the `(*p)++` pointer-in-memory case) -> substring
    match. regA is unchanged by the store, so rebinding to `dest` is always value-sound: the
    store just wrote regA's value to `dest`, so `dest` now equals it."""
    if state.regA == dest:
        return
    if re.fullmatch(r'[A-Za-z_]\w*', dest):
        hit = re.search(r'(?<!\w)' + re.escape(dest) + r'(?!\w)', state.regA)
    else:
        hit = dest in state.regA          # deref dest (indirect self-store)
    if hit:
        state.regA = dest


def decompile(filepath, sub_addr, labels=None, var_names=None, collect=None):
    body_addr, instructions = parse_listing(filepath, sub_addr)
    if not instructions:
        print(f"// sub ${sub_addr:04X} not found in {filepath}")
        return

    state = State()
    # Name the function from its mesen label when one exists (header + the
    # `sub_XXXX(...)` declaration). Falls back to sub_XXXX for anonymous subs.
    fn_name = _label_name(labels, sub_addr) or f"sub_{sub_addr:04X}"
    print(f"// Decompiled from {filepath} sub ${sub_addr:04X}")
    if fn_name != f"sub_{sub_addr:04X}":
        print(f"// ${sub_addr:04X} {fn_name}")
    print(f"// (body @ ${body_addr:04X})")
    print()
    # Real arg count (M3, 2026-06-03 — replaces the hardcoded arg1..arg4). The
    # callee names its params arg1..arg4 = frame slots fp+0x0B/0D/0F/11, reached by
    # the quick arg-opcodes (low nibble 0x0C-0x0F across the load/store/push families).
    # The signature declares exactly the highest arg slot the BODY references — a
    # deterministic, per-sub measure that keeps bank_NN.c and the merged all_banks.c
    # byte-identical (no cross-bank caller pre-pass / global state). It names exactly
    # the args var-walk (M4) will rename. Caller-arity (the $E9 adj byte) is a possible
    # future cross-check but is NOT the authority here (it would couple the two views).
    arg_count = _body_arg_count(instructions)
    params = ", ".join(f"word arg{i}" for i in range(1, arg_count + 1)) or "void"
    params = apply_var_names(params, var_names)   # var-walk: rename args in the signature
    print(f"word {fn_name}({params}) {{")

    # Bug 1.3 fix: if this is an ext-op sub with a certified formula, emit it and stop
    # (DREAM can't fold the 32-bit aux stack, so its generated body is the value-wrong stub).
    _ov = _EXTOP_OVERRIDE.get(sub_addr)
    if _ov:
        _pnames = [seg.split()[-1] for seg in params.split(", ")] if params != "void" else []
        for _ln in _ov[0](_pnames):
            print(f"    {_ln}")
        print("}")
        if collect is not None:
            collect.update(raw=[], structured=[], structured_ungated=[],
                           gated_fallback=False, structurer='override',
                           instructions=instructions, body_addr=body_addr)
        return

    # Value-diamond ternaries: a 2-way conditional whose arms each just LOAD a value and
    # converge — `regA = cond ? vA : vB`. The decode loop folds these to a ternary (else
    # both loads are dropped and only one value survives). The diamond's head branch + the
    # fall-arm jump emit NO line; the bytecode CFG side contracts the same diamonds
    # (vm_cfg.bytecode_cfg) so the gate's `lower(raw) == bytecode_cfg` stays valid.
    from emu_harness.vm.koei.nes import vm_cfg as _vmcfg
    # Consuming-call STACK-PHI repair: at each predecessor's exit to a multi-pred merge, store the
    # call's args (top-N stack entries) into per-merge temps so the shared call pops named temps
    # instead of dropping all but the fall-through pred's pushes (the silent value-drop bug).
    # phi_mat: materialise_addr -> (merge_leader, arity, tag_addr). See vm_cfg.consuming_phis.
    phi_mat = _vmcfg.consuming_phis(instructions)
    # ret_phi: same repair for the RETURN VALUE (regA) at a shared bare `vm_return` merge.
    # ret_phi_sites: site_addr -> merge_leader; the merge's vm_return then returns phi_ret_<merge>.
    ret_phi_sites = _vmcfg.return_phis(instructions)        # site -> (merge, tag_addr)
    ret_phi_merges = {m for (m, _t) in ret_phi_sites.values()}
    # push_phi: same repair for regA at a shared `pushA` merge (the pushed value usually feeds a
    # consuming-call merge, so push_phis + consuming_phis chain). push_phi_sites: site -> merge;
    # the merge's pushA pushes phi_push_<merge>. See vm_cfg.push_phis ($83FA $84B5).
    push_phi_sites = _vmcfg.push_phis(instructions)   # site -> (merge, tag_addr)
    push_phi_merges = set(v[0] for v in push_phi_sites.values())
    # val_phi: regA value-merge at a SIDE-EFFECTING-arm merge whose first op consumes regA -- a
    # `storeA_*` (store regA) or `branch_z/nz` (branch on regA) -- that value_diamonds can't fold.
    # val_phi_sites: site -> (merge, tag_addr); the store/branch then reads phi_val_<merge> (it
    # already reads state.regA, which the materialise sets to the temp). tag_addr buckets the
    # materialise into the PRED's block (the fall pred's else block, NOT the merge) so DREAM places
    # it inside the arm, not after the join. See vm_cfg.value_merge_phis ($A45A).
    val_phi_sites = _vmcfg.value_merge_phis(instructions)
    diamonds = _vmcfg.value_diamonds(instructions)
    dia_by_head = {d['head']: d for d in diamonds}
    dia_fall_jump = {d['fall_jump_addr'] for d in diamonds if d['fall_jump_addr'] is not None}
    dia_taken_start = {d['taken_block']: d for d in diamonds}
    dia_merge = {}
    for d in diamonds:
        dia_merge.setdefault(d['merge'], []).append(d)
    # Short-circuit BOOLEAN regions (K>=2 conditions -> `cond1 && cond2 … ? a : b`). Like
    # diamonds but multi-block: capture each cond block's condition at its branch + each
    # value block's value, then recover the boolean formula and emit the ternary at the merge.
    bregions = _vmcfg.boolean_regions(instructions)
    breg_cond_branch = {}       # branch addr -> (region, cond block)
    breg_merge = {}             # merge addr -> region
    breg_jump = {}              # jumper value-block jump addr -> region
    for r in bregions:
        for ba, cb in r['cond_branch_addrs'].items():
            breg_cond_branch[ba] = (r, cb)
        breg_merge[r['merge']] = r
        breg_jump[r['jump_addr']] = r
    # Short-circuit CONTROL regions: `if (c1 || c2 …) { body }`. Capture each guard's condition
    # at its branch (suppressing the individual `if(ci) goto body` lines), then at the TAIL guard
    # emit ONE compound guard `if (!(c1 || c2 …)) goto skip` — the existing if-then structurer
    # then folds the fall-through body into `if (c1 || c2 …) { body }`. bytecode_cfg contracts the
    # same chain (entry -> {body, skip}) so the gate stays consistent.
    cregions = _vmcfg.control_shortcircuits(instructions)
    cs_cond_branch = {}         # guard branch addr -> (region, guard block)
    cs_skip_targets = set()     # skip blocks the synthetic compound goto jumps to (need labels)
    for r in cregions:
        for ba, cb in r['cond_branch_addrs'].items():
            cs_cond_branch[ba] = (r, cb)
        cs_skip_targets.add(r['skip'])

    # A label is dead once its only branch source is a suppressed diamond/region branch.
    suppressed_src = (set(dia_by_head) | dia_fall_jump
                      | set(breg_cond_branch) | set(breg_jump) | set(cs_cond_branch))

    # First pass: find branch targets (need labels) — EXCLUDING suppressed diamond branches,
    # so a taken-arm label (solely the head's target) and a merge label (solely the fall
    # jump's target) are not emitted as dead labels after the fold.
    branch_targets = set()
    for ins in instructions:
        if ins['addr'] in suppressed_src:
            continue
        if 'branch' in ins['mnemonic'] or 'jump' in ins['mnemonic']:
            m = re.search(r'\$([0-9A-Fa-f]{4})', ins['operand'])
            if m:
                branch_targets.add(int(m.group(1), 16))
        elif ins['mnemonic'] == 'switch':
            # Every case + default target (from the decoded "SWITCH …" comment).
            for t in re.findall(r'=>\$([0-9A-Fa-f]{4})', ins['comment']):
                branch_targets.add(int(t, 16))
    branch_targets |= cs_skip_targets    # the synthetic compound-guard goto needs its skip label

    # Second pass: decompile
    dia_active = None          # the value-diamond currently being folded (None = not in one)
    dia_cond = dia_vt = dia_vt_b = None  # captured cond + the fall-arm (true-path) regA/regB values
    breg_active = None         # the boolean-region currently being folded
    breg_cond_expr, breg_val = {}, {}   # captured per-cond conditions + per-value values
    cs_cond_expr = {}          # control short-circuit: captured per-guard condition exprs
    for ins in instructions:
        state.cur_addr = ins['addr']
        mnem = ins['mnemonic']
        operand = ins['operand']
        comment = ins['comment']
        opcode = ins['bytes'][0]

        # Drift-guard: record data-stack depth at the TOP of each instruction (so
        # per-op Δ = depth[next] - depth[this], which is `continue`-proof). Off by
        # default → no effect on the emitted C. State is fresh per sub, so each
        # sub's trace starts at depth 0 and is bounded by its terminal marker below.
        if SELF_CHECK:
            _STACK_TRACE.append((opcode, len(state.stack)))

        # PHI materialisation (below) runs BEFORE the branch-target label, so a fall-through
        # pred's `phi = <fall value>` lands ABOVE `L_<merge>:` — inside the fall-through block.
        # A JUMPing pred's `goto L_<merge>` then targets the label and BYPASSES that assignment,
        # so its own `phi = <jump value>` (emitted at its jump site) survives to the consumer.
        # Emitting the materialise after the label clobbers every jump arm with the fall value.

        # === Consuming-call STACK-PHI materialisation (pre-dispatch) ===
        # We are at a predecessor's EXIT to a multi-pred merge (its unconditional jump, or the
        # merge's own call for the fall-through pred): the call's args are fully built as the top
        # `arity` stack entries. Store them into per-merge temps `phi_<merge>_<k>` and leave the
        # temp NAMES on the stack, so the merge's call pops the temps (every pred writes the SAME
        # names -> the linear leak of the other preds' args is harmless) and each arg is carried
        # under its own control — sound, no re-evaluation. Net stack Δ 0 (pop N, push N) -> the
        # SELF_CHECK depth trace (recorded above) is unaffected. Fires BEFORE the jump/call.
        if ins['addr'] in phi_mat:
            _phi_M, _phi_n, _phi_tag = phi_mat[ins['addr']]
            if len(state.stack) >= _phi_n:
                _phi_args = [state.stack.pop() for _ in range(_phi_n)]   # top-down = arg1..argN
                _phi_names = [f"phi_{_phi_M:04x}_{_k}" for _k in range(_phi_n)]
                for _k, _v in enumerate(_phi_args):
                    if _v != _phi_names[_k]:                   # skip the trivial phi_k = phi_k
                        # tag with the pred's OWN block addr (_phi_tag) so DREAM buckets the
                        # materialise INTO the arm (the fall pred's else block), not after the join.
                        state.lines.append((_phi_tag, state.indent, f"{_phi_names[_k]} = {_v};"))
                for _nm in reversed(_phi_names):              # push back so pop order = arg1..argN
                    state.stack.append(_nm)

        # === Return-value (regA) PHI materialisation (pre-dispatch) ===
        # At a pred's EXIT to a shared bare `vm_return` (its jump, or the RETURN itself for the
        # fall-through pred), capture regA into phi_ret_<merge> so the return reads the temp instead
        # of dropping every JUMPing pred's value. Reading state.regA consumes a pending call exactly
        # once (no re-eval); all preds write the SAME name. See vm_cfg.return_phis.
        if ins['addr'] in ret_phi_sites:
            _rm, _rtag = ret_phi_sites[ins['addr']]
            _rnm = f"phi_ret_{_rm:04x}"
            _rv = state.regA                              # consume any pending call exactly once
            if _rv != _rnm:
                # tag with the pred's OWN block addr (_rtag) so DREAM buckets the materialise INTO
                # the arm (jump/branch pred = the jump/branch addr; fall pred = its last-instr addr,
                # the fall/else block) instead of after the join. See vm_cfg.return_phis.
                state.lines.append((_rtag, state.indent, f"{_rnm} = {_rv};"))
            state.regA = _rnm

        # === Push (regA) PHI materialisation (pre-dispatch) ===
        # At a pred's EXIT to a shared `pushA` merge (its jump, or the pushA itself for the
        # fall-through pred), capture regA into phi_push_<merge> so the push carries the temp
        # instead of dropping every JUMPing pred's regA. Reading regA consumes a pending call
        # exactly once; all preds write the SAME name. See vm_cfg.push_phis.
        if ins['addr'] in push_phi_sites:
            _pm, _ptag = push_phi_sites[ins['addr']]
            _pnm = f"phi_push_{_pm:04x}"
            _pv = state.regA                              # consume any pending call exactly once
            if _pv != _pnm:
                # tag with the pred's OWN block addr (_ptag) so DREAM places it in the arm, not after
                # the join (mirrors return_phis/value_merge_phis). Jump preds: _ptag == ins['addr'].
                state.lines.append((_ptag, state.indent, f"{_pnm} = {_pv};"))
            state.regA = _pnm

        # === Value-merge (regA) PHI materialisation (pre-dispatch) ===
        # At a pred's EXIT to a SIDE-EFFECTING-arm merge whose first op consumes regA (its jump, or
        # the store/branch itself for the fall-through pred), capture regA into phi_val_<merge> so
        # the store/branch reads the temp instead of dropping every JUMPing pred's value. Reading
        # regA consumes a pending call exactly once (no re-eval, rng advances as the bytecode does);
        # all preds write the SAME name. See vm_cfg.value_merge_phis ($A45A $A4FD store, $A523 branch).
        if ins['addr'] in val_phi_sites:
            _vm, _vtag = val_phi_sites[ins['addr']]
            _vnm = f"phi_val_{_vm:04x}"
            _vv = state.regA                              # consume any pending call exactly once
            if _vv != _vnm:
                # tag the materialise with the pred's OWN block addr (_vtag), not the firing site:
                # for the fall-through pred that is the fall (else) block, so DREAM buckets it INTO
                # the else arm instead of after the join. Jump preds: _vtag == ins['addr'] (no-op
                # vs the old state.emit). See vm_cfg.value_merge_phis.
                state.lines.append((_vtag, state.indent, f"{_vnm} = {_vv};"))
            state.regA = _vnm

        # Label for branch targets — AFTER the phi materialise (see note above) so a jump arm's
        # `goto L_<merge>` lands below the fall-through pred's `phi = …` instead of clobbering on it.
        if ins['addr'] in branch_targets:
            state.lines.append((ins['addr'], 0, f"L_{ins['addr']:04X}:"))

        # === Short-circuit CONTROL folding (`if (c1 || c2 …) { body }`) ===
        # Capture each guard's condition at its branch (suppressing the individual
        # `if(ci) goto body`); at the TAIL guard emit ONE compound `if (!(c1||c2…)) goto skip`,
        # which the if-then structurer folds into `if (c1||c2…) { body }`. The guard blocks must
        # be side-effect-free (detector-enforced) so hoisting the conditions is sound.
        if ins['addr'] in cs_cond_branch:
            region, guard = cs_cond_branch[ins['addr']]
            cs_cond_expr[guard] = state.regA
            if guard == region['tail']:
                formula = _vmcfg.recover_bool_formula(region, cs_cond_expr, region['body'])
                state.emit(f"if (!({formula})) goto L_{region['skip']:04X};")
                cs_cond_expr = {}
            continue

        # === Short-circuit boolean-region folding (K>=2 conditions) ===
        # Capture each cond block's condition at its branch (READ consumes a pending call),
        # the jumper value at its jump, the faller value at the merge; then recover the
        # boolean formula and emit `(formula ? a : b)`. Disjoint from the diamond path below.
        if ins['addr'] in breg_cond_branch:
            region, cblock = breg_cond_branch[ins['addr']]
            breg_active = region
            breg_cond_expr[cblock] = state.regA
            continue
        if breg_active is not None and ins['addr'] == breg_active['jump_addr']:
            breg_val[breg_active['jumper']] = state.regA
            continue
        if breg_active is not None and ins['addr'] == breg_active['merge']:
            r = breg_active
            breg_val[r['faller']] = state.regA
            fa = _vmcfg.recover_bool_formula(r, breg_cond_expr, r['va'])
            fb = _vmcfg.recover_bool_formula(r, breg_cond_expr, r['vb'])
            if len(fb) <= len(fa):                      # pick the simpler (positive) formula
                state.regA = _bool_ternary_expr(fb, breg_val[r['vb']], breg_val[r['va']])
            else:
                state.regA = _bool_ternary_expr(fa, breg_val[r['va']], breg_val[r['vb']])
            breg_active, breg_cond_expr, breg_val = None, {}, {}
            # fall through: the merge instruction consumes regA (the folded expression)

        # === Value-diamond ternary folding ===
        # Order matters: capture the fall-arm value when we ENTER the taken arm (before its
        # load overwrites regA), and build the ternary when we REACH the merge (regA now
        # holds the taken-arm value). Both run BEFORE the elif chain processes this instr.
        if dia_active is not None:
            if ins['addr'] == dia_active['taken_block']:
                dia_vt = state.regA                       # fall-arm (entered first) regA
                dia_vt_b = state.regB                      #                       ... regB
            if ins['addr'] == dia_active['merge']:
                _vf, _vf_b = state.regA, state.regB        # taken-arm regA / regB
                _pol, _cond = dia_active['polarity'], dia_cond
                # Fold BOTH registers: regA-only diamonds collapse regB to its shared value;
                # the $AF46 regB-diamond collapses regA and carries the regB ternary into the
                # merge's `add`, so the dropped `province_to_map_section` arm survives. When BOTH
                # differ (a sorted-pair index like `min*54 + max`), the cond would appear in two
                # ternaries — HOIST it into a temp so a call-valued cond runs ONCE (duplicating
                # the call text = double side effect) and the pure case reads without repetition.
                if dia_vt != _vf and dia_vt_b != _vf_b:
                    _cond = f"dsel_{dia_active['merge']:04x}"
                    state.emit(f"{_cond} = {dia_cond};")
                state.regA = _arm_ternary(_cond, dia_vt, _vf, _pol)
                state.regB = _arm_ternary(_cond, dia_vt_b, _vf_b, _pol)
                dia_active = None                         # merge instr now consumes the ternary(ies)
        # Head conditional: capture the cond (regA), start folding, emit NO branch line.
        if ins['addr'] in dia_by_head:
            dia_active = dia_by_head[ins['addr']]
            dia_cond = state.regA
            continue
        # Fall-arm terminating jump: emit NO goto (the arms are folded into the ternary).
        if dia_active is not None and ins['addr'] == dia_active['fall_jump_addr']:
            continue

        # === Frame local loads ===
        if mnem == 'loadA_local_pos':
            state.regA = local_name(opcode & 0x0F, 'pos')
        elif mnem == 'loadB_local_pos':
            state.regB = local_name(opcode & 0x0F, 'pos')
        elif mnem == 'loadA_local_neg':
            state.regA = local_name(opcode & 0x0F, 'neg')
        elif mnem == 'loadB_local_neg':
            state.regB = local_name(opcode & 0x0F, 'neg')
        elif mnem == 'storeA_local_pos':
            name = local_name(opcode & 0x0F, 'pos')
            state.emit(f"{name} = {state.regA};")
            _rebind_self_store(state, name)
        elif mnem == 'storeA_local_neg':
            name = local_name(opcode & 0x0F, 'neg')
            state.emit(f"{name} = {state.regA};")
            state.locals[opcode & 0x0F] = state.regA
            _rebind_self_store(state, name)
        elif mnem == 'push_local_neg':
            # $30-$3B PUSH_quick: push a frame LOCAL's value onto the data stack.
            # (Oracle-confirmed: $38 pushes local8, sp -= 2 — not a store of regB.)
            state.stack.append(local_name(opcode & 0x0F, 'neg'))
        elif mnem == 'push_local_pos':
            # $3C-$3F PUSH_quick: push a frame ARG's value (arg-forwarding to a call).
            # Oracle-confirmed on the syscall wrapper stubs ($CBBD memcpy_banked forwarder):
            # $3C pushes arg1 (fp+0x0B) ... $3F pushes arg4 (fp+0x11).
            state.stack.append(local_name(opcode & 0x0F, 'pos'))

        # === Immediates ===
        elif mnem == 'loadA_imm_word':
            state.regA = _imm_word_repr(_op_hex(operand), labels)
        elif mnem == 'loadB_imm_word':
            state.regB = _imm_word_repr(_op_hex(operand), labels)
        elif mnem == 'loadA_imm_byte':
            m = re.search(r'\+?(\d+)', operand)
            state.regA = str(int(m.group(1)) if m else 0)
        elif mnem == 'loadB_imm_byte':
            # $8B BYTE_LOADR_imm1 sign-extends its byte (live handlers, na1 / gk / uw: FF -> $FFFF).
            # This parse dropped the sign, printing `x != 1` for the bytecode's `x != -1` (NA1 revisit).
            state.regB = str(_op_int(operand))
        elif mnem == 'setB_imm4':
            state.regB = str(opcode & 0x0F)
        elif mnem == 'addA_imm4':
            state.regA = f"({state.regA} + {opcode & 0x0F})"
        elif mnem == 'addA_imm_sbyte':
            state.regA = f"({state.regA} + {_op_int(operand)})"
        elif mnem == 'loadA_imm_sbyte':
            state.regA = str(_op_int(operand))
        elif mnem == 'loadB_imm_sbyte':
            state.regB = str(_op_int(operand))
        elif mnem == 'loadA_mem_word':
            state.regA = _mem_name(operand, _op_hex(operand), labels)
        elif mnem == 'loadB_mem_word':
            state.regB = _mem_name(operand, _op_hex(operand), labels)
        elif mnem in _UNARY_A:
            # Unary register ops -> regA = f(regA). Table-driven; see _UNARY_A.
            state.regA = _UNARY_A[mnem].format(A=state.regA)
        elif mnem in _BINOP:
            # All binary register ops -> regA = (regA <op> regB). Table-driven; see _BINOP.
            op, cast = _BINOP[mnem]
            a = f"(unsigned){state.regA}" if cast != 'none' else state.regA
            b = f"(unsigned){state.regB}" if cast == 'both' else state.regB
            state.regA = f"({a} {op} {b})"

        # === Pointer dereferences (loads are table-driven via _UNARY_A above) ===
        elif mnem == 'storeA_ind_byte':
            # regB has the pointer (from stack); regA is the value
            ptr = state.stack.pop() if state.stack else state.regB
            dest = f"*(byte*)({ptr})"
            state.emit(f"{dest} = {state.regA};")
            _rebind_self_store(state, dest)
        elif mnem == 'storeA_ind_word':
            ptr = state.stack.pop() if state.stack else state.regB
            dest = f"*(word*)({ptr})"
            state.emit(f"{dest} = {state.regA};")
            _rebind_self_store(state, dest)

        # === Stack ===
        elif mnem == 'pushA':
            # A push-phi merge pushes its temp (set by whichever pred control-flowed here), not
            # the linearly-stale regA — see push_phi_sites above (mirrors the return-phi merge).
            if ins['addr'] in push_phi_merges:
                state.stack.append(f"phi_push_{ins['addr']:04x}")
            else:
                state.stack.append(state.regA)
        elif mnem == 'popB':
            if state.stack:
                state.regB = state.stack.pop()
        elif mnem == 'push_imm4':
            state.stack.append(str(opcode & 0x0F))
        elif mnem == 'push_imm_sbyte':
            state.stack.append(str(_op_int(operand)))
        elif mnem == 'op_8D_sbyte':
            # $8D BYTE_PUSH_imm1 — push a 1-byte immediate (operand rendered "+NN").
            state.stack.append(str(_op_int(operand)))
        elif mnem == 'push_imm_word':
            # $8E PUSH_imm2 — push a 2-byte immediate (usually a table/struct
            # pointer; the label names that address, so push it by name).
            label_m = re.search(r'\(([a-z_][a-z0-9_]*)\)', operand)
            state.stack.append(label_m.group(1) if label_m else _imm_word_repr(_op_hex(operand), labels))
        elif mnem == 'PUSH_abs':
            # $AA PUSH_abs — push the VALUE stored at an absolute address.
            state.stack.append(_mem_name(operand, _op_hex(operand), labels))

        # === Logic / misc (binary & unary register ops table-driven above) ===
        elif mnem == 'swap_AB':
            state.regA, state.regB = state.regB, state.regA
        elif mnem == 'loadA_imm4':
            # $40-$4F LOADL_qimm: regL = low nibble (0..15). $40 == 0 (was a "clearA"
            # special case); $41-$4F carry a real immediate the old handler dropped.
            state.regA = str(opcode & 0x0F)

        # === Branches & control flow ===
        elif mnem == 'branch_z_abs':
            m = re.search(r'\$([0-9A-Fa-f]{4})', operand)
            tgt = int(m.group(1), 16) if m else 0
            state.emit(f"if (!({state.regA})) goto L_{tgt:04X};")
        elif mnem == 'branch_nz_abs':
            m = re.search(r'\$([0-9A-Fa-f]{4})', operand)
            tgt = int(m.group(1), 16) if m else 0
            state.emit(f"if ({state.regA}) goto L_{tgt:04X};")
        elif mnem == 'jump_abs':
            m = re.search(r'\$([0-9A-Fa-f]{4})', operand)
            tgt = int(m.group(1), 16) if m else 0
            state.flush_pending()  # goto doesn't read regA — emit a pending call before it
            state.emit(f"goto L_{tgt:04X};")
        elif mnem == 'vm_return':
            # A return-phi merge returns its temp (set by whichever pred control-flowed here),
            # not the linearly-stale regA — see ret_phi_sites above.
            if ins['addr'] in ret_phi_merges:
                state.emit(f"return phi_ret_{ins['addr']:04x};")
            else:
                state.emit(f"return {state.regA};")
        elif mnem == 'switch':
            # vm-disasm decoded the jump table into the comment:
            #   "SWITCH <key>=>$<tgt> ... default=>$<tgt>"
            # regA holds the switched value. Emit a C switch of goto-cases.
            pairs = re.findall(r'(-?\d+)=>\$([0-9A-Fa-f]{4})', comment)
            dflt = re.search(r'default=>\$([0-9A-Fa-f]{4})', comment)
            if pairs:
                state.emit(f"switch ({state.regA}) {{")
                state.indent += 1
                for key, tgt in pairs:
                    state.emit(f"case {key}: goto L_{int(tgt, 16):04X};")
                if dflt:
                    state.emit(f"default: goto L_{int(dflt.group(1), 16):04X};")
                state.indent -= 1
                state.emit("}")
            else:
                state.emit(f"// TODO: switch {operand}")

        # === Host calls ===
        elif mnem == 'host_call' or mnem == 'host_call_simple':
            # Operand format: "$XXXX [label] {kind}, $YY" or "$XXXX {kind}"
            m = re.search(r'\$([0-9A-Fa-f]{4})', operand)
            tgt = int(m.group(1), 16) if m else 0
            # Extract bytes-popped from "...{kind}, $NN" suffix — N bytes = N/2 args
            ptr1_m = re.search(r',\s*\$([0-9A-Fa-f]+)\s*$', operand)
            bytes_popped = int(ptr1_m.group(1), 16) if ptr1_m else 0
            # Try to extract label: prefer the disasm operand's inline (label),
            # else fall back to the mesen labels dict, else sub_XXXX.
            label_m = re.search(r'\(([a-z_][a-z0-9_]*)\)', operand)
            if label_m:
                fname = label_m.group(1)
            else:
                fname = _label_name(labels, tgt) or f"sub_{tgt:04X}"
            # Known host_call arities (override the bytes-popped estimate)
            arity_map = {
                0xCBCD: 1,  # sqrt_int(x)
                0xD6B8: 3,  # math32_3arg(a,b,c)
                0xD6DE: 2,  # math32_2arg(a,b)
                0xD70D: 2,  # pct_op(b, p) — base, percent
                0xCA52: 1,  # rng_mod(max)
                0xD7CD: 1,  # daimyo_record_ptr(idx)
            }
            arity = arity_map.get(tgt, bytes_popped // 2)
            # Pop args in LIFO (top-of-stack first). The VM pushes call args
            # right-to-left, so the LAST-pushed word is the callee's arg1 (frame slot
            # fp+0x0B) — i.e. the FIRST popped here. Displaying them in pop order
            # therefore matches the callee's param numbering arg1, arg2, …, which is
            # what var-walk caller-propagation needs (M3 reverse-push fix, 2026-06-03;
            # this list used to be .reverse()d, inverting call-site order vs the body
            # and forcing the mental flip in math32_3arg(c,b,a) etc.).
            args = [state.stack.pop() if state.stack else "/*stack underflow*/ regA"
                    for _ in range(arity)]
            args = _apply_arg_enums(fname, args)
            state.set_call(f"{fname}({', '.join(args)})" if args else f"{fname}()")

        # === Indirect host calls — VM opcodes $EA / $DD (call through regA) ===
        # The callee address is computed (in regA), not an inline label. $EA carries a
        # ptr1-adjust byte (= bytes popped, like $E9); $DD takes none (like $AC). Args
        # come off the VM stack, same as host_call.
        elif mnem in ('host_call_indirect', 'host_call_indirect_simple'):
            callee = state.regA
            bp = re.search(r'\$([0-9A-Fa-f]+)', operand)
            arity = (int(bp.group(1), 16) // 2) if bp else 0
            # Pop in LIFO so arg1 (last-pushed, fp+0x0B) prints first — see the
            # host_call note above (M3 reverse-push fix).
            args = [state.stack.pop() if state.stack else "/*stack underflow*/ regA"
                    for _ in range(arity)]
            state.set_call(f"(*({callee}))({', '.join(args)})")

        # === Extended (32-bit / bitfield) ops — VM opcode $B7 ('ext_op') ===
        # The operand is the ext-op INDEX byte ("$NN"). Resolve it through EXT_OP_TABLE
        # to a handler address, then name it via the mesen labels (Session A walk).
        # ext_ops form a postfix stack-machine sequence on the A/B register file
        # ($08-$0F), so we name the operation rather than fake an f(regA,regB) call.
        # EXCEPT $18/$19 (load_a32/b32_from_ptr3): these carry a 4-byte inline LE
        # immediate (vm-disasm.py gives the instruction length 6), so render the load.
        elif mnem == 'ext_op':
            m = re.search(r'\$([0-9A-Fa-f]+)', operand)
            idx = int(m.group(1), 16) if m else -1
            b = ins['bytes']
            if idx in (0x18, 0x19) and len(b) >= 6:
                imm = b[2] | (b[3] << 8) | (b[4] << 16) | (b[5] << 24)
                lit = str(imm) if imm < 256 else f"0x{imm:08X}"
                if idx == 0x18:   # load_a32_from_ptr3
                    state.regA = lit
                else:             # $19 load_b32_from_ptr3
                    state.regB = lit
            else:
                haddr = EXT_OP_TABLE.get(idx)
                if haddr:
                    name = _label_name(labels, haddr) or f"ext_op_{haddr:04X}"
                    state.emit(f"// ext_op {name}")
                else:
                    state.emit(f"// ext_op ${idx:02X} (unmapped)")

        # === General frame-local access (vm_fp + signed offset) ===
        # Decoded from the handlers $EA34-$EB25 (see _WORD_SLOT/frame_* helpers).
        # $81/$82 word load -> A; $83/$84 word load -> B; $85/$86 store A -> word;
        # $87/$88 push word; $A0-$A3 byte load/store/push; $90 add-imm16; $DE/$DF
        # frame-address; $50 setB=0.
        elif mnem in ('op_81_byte', 'op_82_bb'):
            state.regA = frame_word(_signed_operand(operand))
        elif mnem in ('op_83_byte', 'op_84_bwb'):
            state.regB = frame_word(_signed_operand(operand))
        elif mnem in ('op_85_byte', 'op_86_bwb'):
            dest = frame_word(_signed_operand(operand))
            state.emit(f"{dest} = {state.regA};")
            _rebind_self_store(state, dest)
        elif mnem in ('op_87_byte', 'op_88_bwb'):
            state.stack.append(frame_word(_signed_operand(operand)))
        elif mnem == 'op_A0_A3_byte':
            off = _signed_operand(operand)
            sel = opcode & 0x03
            if sel == 0:      # $A0 byte load -> A
                state.regA = frame_byte(off)
            elif sel == 1:    # $A1 byte load -> B
                state.regB = frame_byte(off)
            elif sel == 2:    # $A2 store A -> byte
                dest = frame_byte(off)
                state.emit(f"{dest} = {state.regA};")
                _rebind_self_store(state, dest)
            else:             # $A3 push byte
                state.stack.append(frame_byte(off))
        elif mnem == 'loadA_frameaddr':
            state.regA = frame_addr(_signed_operand(operand))
        elif mnem == 'loadB_frameaddr':
            state.regB = frame_addr(_signed_operand(operand))
        elif mnem == 'op_90_bb':
            # $90: regA += imm16 (handler falls into the addA tail at $EADC).
            val = _signed_operand(operand)
            sign = '+' if val >= 0 else '-'
            state.regA = f"({state.regA} {sign} {abs(val)})"
        elif mnem == 'setB_imm4_50':
            # $50: dedicated handler stores Y(=0) into regB.
            state.regB = "0"

        # === Misc register ops (binary & unary register ops table-driven above) ===
        elif mnem == 'nop_B2':
            # $B2 is a NOP (no stack/reg effect, length 1) — ORACLE-CONFIRMED 2026-06-03
            # via a synthetic micro-test through the real ROM handler (dSP=0, regL/regR
            # unchanged, pc+1). Resolves the M2 NOP-vs-PUSHL dispute: it was modeled here
            # as PUSHL(regA), which was wrong (harmless only because $B2 is never emitted
            # in this ROM — 0 occurrences across banks 0/1/2/15). OPCODE_INFO / v2 toml
            # ("NOP") were right. Left as an explicit no-op so the model is correct if a
            # $B2 ever turns up. See vm-stack-audit.py `validate_nop_b2`.
            pass

        # === Absolute byte memory (byte mirrors of loadA_mem_word/store) ===
        elif mnem == 'loadA_mem_byte':
            state.regA = _mem_name(operand, _op_hex(operand), labels)
        elif mnem == 'storeA_mem_word':
            # $A8 STORE_abs — store regA's WORD to an absolute address. (Was wrongly
            # mapped to loadA_mem_word: every $A8 store rendered as a phantom read.
            # PROVEN $A8=store by the year++ at $A48E LOADL_abs / INC / $A492 STORE_abs.)
            # regA is the SOURCE and is left unchanged by the store.
            dest = _mem_name(operand, _op_hex(operand), labels)
            state.emit(f"{dest} = {state.regA};")
            _rebind_self_store(state, dest)
        elif mnem == 'storeA_mem_byte':
            dest = _mem_name(operand, _op_hex(operand), labels)
            state.emit(f"{dest} = {state.regA};")
            _rebind_self_store(state, dest)

        # === Misc ===
        elif mnem == 'nop':
            pass                         # VM no-op slot (opcode -> default handler); emit nothing
        else:
            state.emit(f"// TODO: {mnem} {operand}".strip())

    if SELF_CHECK:           # terminal marker: closes this sub's trace (Δ of its last op)
        _STACK_TRACE.append((None, len(state.stack)))

    state.flush_pending()  # function end: don't lose a trailing discarded call

    # Post-pass: detect simple if/else patterns and structure them.
    # Pattern A (if-then):
    #     if (!(C)) goto L_X;
    #     <body>            ← straight-line, no branches
    #   L_X:
    # Becomes:
    #     if (C) {
    #         <body>
    #     }
    # Pattern B (if-then-else):
    #     if (!(C)) goto L_X;
    #     <then-body>
    #     goto L_Y;
    #   L_X:
    #     <else-body>
    #   L_Y:
    structured_ungated, gated_fallback = state.lines, False
    structurer = None
    if STRUCTURE and STRUCTURE_DREAM:
        # CFG epic rung-6 CUTOVER — DREAM PRIMARY, V2 FALLBACK. Try DREAM first: it
        # self-validates with its AST-native gate inside structure_dream and returns the
        # raw `state.lines` UNCHANGED when it can't own the sub (out of scope / gate reject /
        # honest over-dup). On that fallback signal, route the sub to the V2 region reducer,
        # gated by structured_equivalent exactly as the STRUCTURE_V2 path does. So every sub
        # has one owner: DREAM where its gate passes, V2 otherwise — the hybrid code oracle.
        # `structurer` records that owner so the hybrid gate (vm-cfg-gate --dream) re-validates
        # each sub with the RIGHT relation (DREAM via dream_equivalent_ast, V2 via structured_equivalent).
        structured = structure_dream(state.lines, instructions)
        if structured is not state.lines:
            structured_ungated, structurer = structured, 'dream'   # DREAM-owned (gate-validated)
        else:
            structured = structure_v2(state.lines, instructions)   # V2 fallback
            structured_ungated, structurer = structured, 'v2'
            if structured is not state.lines:
                from emu_harness.vm.koei.nes import vm_cfg
                _leaders = vm_cfg.bytecode_cfg(instructions)[1]
                r, _nr, _ns = vm_cfg.structured_equivalent(state.lines, structured, _leaders)
                if not r:
                    structured, gated_fallback, structurer = state.lines, True, 'raw'
            else:
                structurer = 'raw'                                 # V2 passthrough == raw goto
    elif STRUCTURE and STRUCTURE_V2:
        # CFG epic V2 — REGION REDUCER. A SINGLE bottom-up reducer (vm_reduce, behind
        # structure_v2) replaces the three template passes. It detects irreducibility
        # structurally and emits honest gotos there itself, so the gate is a pure
        # backstop for rule BUGS — one pass, no progressive peel (nothing to peel).
        structured = structure_v2(state.lines, instructions)
        structured_ungated = structured
        if structured is not state.lines:
            from emu_harness.vm.koei.nes import vm_cfg
            _leaders = vm_cfg.bytecode_cfg(instructions)[1]
            r, _nr, _ns = vm_cfg.structured_equivalent(state.lines, structured, _leaders)
            if not r:
                structured, gated_fallback = state.lines, True
    elif STRUCTURE:
        # Template menagerie (V1) — RETIRED from canonical emit (DREAM is now primary,
        # 2026-06-10); reached only when STRUCTURE alone is set (the 114-suite / default
        # gate front-end regression path), never for the committed decompiled/*.c.
        # Phase 1: fold loops first (while), then the if/else inside the bodies, and LAST
        # inline switch case bodies (Phase 3). Switches go last so structure_lines folds the
        # case bodies on clean goto lines BEFORE the case labels exist — else its if/else span
        # can cross a `case:` boundary and swallow the next case (lexically wrong C the
        # address-based gate would still accept).
        from emu_harness.vm.koei.nes import vm_cfg as _vc
        _case_barriers = frozenset(t for d in _vc.switch_dispatches(instructions)
                                   for _k, t in d['key_targets'])
        looped = structure_loops(state.lines, instructions)
        ifelse = structure_lines(looped, _case_barriers)
        structured = structure_switches(ifelse, instructions)
        # CFG epic Phase 1: GATE the structuring. structure_lines / structure_loops /
        # structure_switches are not always CFG-preserving (a fold can mis-route an irreducible
        # region — structure_lines once nested num_to_ascii's recursive branch inside an `if`,
        # returning garbage). So verify the structured form induces the SAME control-flow graph
        # as the raw witness (== the bytecode); if not, fall back. PROGRESSIVE fallback: peel the
        # most-speculative pass off and retry, so one bad fold (a loop OR a switch) never costs a
        # sub its OTHER valid folds. Order: full -> drop switch -> drop loop+switch -> honest goto.
        structured_ungated = structured
        if structured is not state.lines:
            from emu_harness.vm.koei.nes import vm_cfg
            _bc, _leaders = vm_cfg.bytecode_cfg(instructions)

            def _ok(cand):
                r, _nr, _ns = vm_cfg.structured_equivalent(state.lines, cand, _leaders)
                return r

            ok = _ok(structured)
            if not ok and structured is not ifelse:          # the SWITCH inline may be the culprit
                if _ok(ifelse):
                    structured, ok = ifelse, True
            if not ok and looped is not state.lines:          # else the LOOP fold may be
                alt = structure_lines(state.lines)
                if _ok(alt):
                    structured, ok = alt, True
            if not ok:
                structured = state.lines
                gated_fallback = True
    else:
        structured = state.lines

    # Hand the raw + (gated) structured control-flow line lists and the decoded
    # instructions back to a caller (the equivalence gate) without disturbing the
    # print path. Pre-annotation lines suffice — the annotate passes rewrite
    # expressions/operands, never the goto/label/keyword scaffolding the CFG reads.
    if collect is not None:
        collect['raw'] = list(state.lines)
        collect['structured'] = list(structured)
        collect['structured_ungated'] = list(structured_ungated)
        collect['gated_fallback'] = gated_fallback
        collect['structurer'] = structurer      # 'dream' | 'v2' | 'raw' | None (non-DREAM run)
        collect['instructions'] = instructions
        collect['body_addr'] = body_addr

    # Emit lines with field-name annotation
    annotated = []
    for addr, indent, text in structured:
        if CPP_VAR:
            text = annotate_field_access(text, CPP_VAR, CPP_FIELDS)
        for n in range(1, 5):
            text = annotate_field_access(text, f"arg{n}", ARG_FIELDS)
        text = annotate_array_access(text, labels)
        text = annotate_syscall(text)
        text = annotate_bank_call(text)
        text = simplify_expression(text)
        # After simplify: its rules match paren-free operands only, and a record access has none
        # once rendered, so running first would let it rewrite `!((x > 0))` that it never touched.
        text = annotate_record_access(text, labels)
        annotated.append((addr, indent, text))
    for addr, indent, text in annotate_record_locals(annotated):     # needs the whole sub
        text = apply_var_names(text, var_names)   # var-walk: LAST, so arg1->output -> fief->output
        if text.endswith(':') and not text.startswith('//'):
            # `L_XXXX:` goto labels sit at column 0 (existing convention); Phase-3 inlined
            # `case N:` / `default:` labels live INSIDE the switch, so keep their indent.
            if re.match(r'(?:case -?\d+|default):$', text):
                print(f"{'    ' * indent}{text}")
            else:
                print(f"{text}")
        elif text.startswith('//'):
            print(f"{'    ' * indent}{text}")
        else:
            print(f"{'    ' * indent}{text}    // ${addr:04X}" if addr else f"{'    ' * indent}{text}")
    print("}")


# Simplification rewrites: undo doubly-negated conditions, fold (X + 0), etc.
SIMPLIFY_RULES = [
    (re.compile(r'\(0 - ([a-zA-Z_0-9]+)\)'), r'(-\1)'),
    (re.compile(r'\(([a-zA-Z_0-9]+) \+ 0\)'), r'\1'),
    (re.compile(r'!\(\(([^()]+) < ([^()]+)\)\)'), r'(\1 >= \2)'),
    (re.compile(r'!\(\(([^()]+) > ([^()]+)\)\)'), r'(\1 <= \2)'),
    (re.compile(r'!\(\(([^()]+) == ([^()]+)\)\)'), r'(\1 != \2)'),
    (re.compile(r'!\(\(([^()]+) != ([^()]+)\)\)'), r'(\1 == \2)'),
]


_CMP_INVERSE = {'<': '>=', '>': '<=', '<=': '>', '>=': '<', '==': '!=', '!=': '=='}
# Binary operators that bind tighter than a comparison: a lone top-level comparison among
# these is the condition's root, so flipping it negates the whole condition.
_TIGHTER_THAN_CMP = {'+', '-', '*', '/', '%', '<<', '>>'}


def _is_wrapped(s):
    """True when `s` is one paren group: the '(' at 0 closes at the last char.
    `(a == 1) && (b == 2)` starts and ends with a paren but is not wrapped."""
    if not (s.startswith('(') and s.endswith(')')):
        return False
    depth = 0
    for i, c in enumerate(s):
        depth += (c == '(') - (c == ')')
        if depth == 0 and i < len(s) - 1:
            return False
    return True


def _top_level_ops(s):
    """[(start, op)] for the space-bounded binary operators at paren depth 0. The engine
    prints a binary operator between spaces and a unary one without (`a & b`, `&arg2`)."""
    ops, depth = [], 0
    for i, c in enumerate(s):
        depth += (c == '(') - (c == ')')
        if depth == 0 and c == ' ':
            m = re.match(r' ([-+*/%<>=!&|^?:]+) ', s[i:])
            if m:
                ops.append((i + 1, m.group(1)))
    return ops


def invert_condition(cond):
    """Negate a condition expression for early-return rewrite. Parses the top level: a
    wrapped `!(X)` inverts to X, a lone top-level comparison is flipped, anything else
    (`&&`, `||`, `?:`, `&`, a comparison inside a call) becomes `!(...)`. The textual
    version split at the first comparison anywhere, so `!(A || B)` came out `!(~A || B)`."""
    cond = cond.strip()
    while _is_wrapped(cond):
        cond = cond[1:-1].strip()
    if cond.startswith('!') and _is_wrapped(cond[1:]):
        inner = cond[1:]
        while _is_wrapped(inner):
            inner = inner[1:-1].strip()
        return f"({inner})"
    ops = _top_level_ops(cond)
    cmps = [(i, op) for i, op in ops if op in _CMP_INVERSE]
    if len(cmps) == 1 and all(op in _TIGHTER_THAN_CMP for _, op in ops if op not in _CMP_INVERSE):
        i, op = cmps[0]
        return f"({cond[:i].rstrip()} {_CMP_INVERSE[op]} {cond[i + len(op):].lstrip()})"
    return f"!({cond})"


def simplify_expression(text):
    """Apply simple algebraic and logical simplifications."""
    for pat, rep in SIMPLIFY_RULES:
        for _ in range(3):  # apply up to 3 times for nested
            new = pat.sub(rep, text)
            if new == text: break
            text = new
    return text


def _parse_branch_line(text):
    """(kind, target_addr, cond) for a control line, else (None, None, None).
    kind: 'ifgoto' (cond -> target) / 'goto' (uncond -> target)."""
    t = text.strip()
    m = re.match(r'if \((.+)\) goto L_([0-9A-Fa-f]{4});$', t)
    if m:
        return 'ifgoto', int(m.group(2), 16), m.group(1)
    m = re.match(r'goto L_([0-9A-Fa-f]{4});$', t)
    if m:
        return 'goto', int(m.group(1), 16), None
    return None, None, None


def _negate_cond(cond):
    """Loop-continue condition = negation of a 'taken -> exit' test. Unwrap an
    explicit `!(X)` to X; otherwise flip the comparison (or fall back to !(...))."""
    return invert_condition(cond)


def _is_pure_exit_block(b, cfg, loop):
    """A block none of whose successors re-enter the loop — i.e. it only returns
    (EXIT) or jumps OUT of the loop. Such a block sitting lexically inside the loop's
    listing span (e.g. a top-test header's fall-through `return`, emitted between the
    test and the body) can be safely RELOCATED to after the loop: the CFG lowering is
    address-keyed, so moving its lines past the `}` leaves its edges unchanged."""
    from emu_harness.vm.koei.nes import vm_cfg
    return all(s is vm_cfg.EXIT or s not in loop for s in cfg.get(b, ()))


def _loop_single_entry(cfg, loop, h, exit_e):
    """Shared loop validation: the only way IN is the header h (reducible single-entry),
    and every way OUT is either the structural exit `exit_e` (the while-condition's
    false branch) or a PURE-EXIT block — a mid-body `return`/`break` that just leaves
    (incl. one routing to exit_e). Multiple such pure exits are fine: each stays an
    honest `return`/`break`/`goto` and the gate validates the edges. A non-loop
    successor that is NOT exit_e and NOT pure-exit (i.e. it re-enters live code) is a
    genuine second structural exit we don't structure yet -> reject."""
    from emu_harness.vm.koei.nes import vm_cfg
    for n in loop:
        for s in cfg[n]:
            if s is vm_cfg.EXIT or s in loop or s == exit_e:
                continue
            if not _is_pure_exit_block(s, cfg, loop):
                return False
    for n in loop:
        if n != h and any(n in cfg[p] and p not in loop for p in cfg):
            return False
    return True


def _emit_break_continue(out, leaders, loop, h, exit_e):
    """CFG epic, Phase 2: inside a just-folded loop body, rewrite the gotos that the
    structure now makes redundant into `break;` / `continue;`:
      goto L_exit       -> break;            if (C) goto L_exit -> if (C) break;
      goto L_header     -> continue;         if (C) goto L_header -> if (C) continue;
    Only lines whose block is IN the loop are touched (a same-target goto from OUTSIDE
    the loop must stay a goto), and only the loop's own exit / header addresses match —
    every other in-body goto (to a sibling label) is left alone. The CFG gate is the
    backstop: break lowers to the loop exit, continue to the header, so a mis-rewrite
    would change the lowered CFG and be rejected (reverting the whole sub to goto)."""
    from emu_harness.vm.koei.nes import vm_cfg
    block_of = vm_cfg._block_of_fn(leaders)
    exit_lbl = None if exit_e is vm_cfg.EXIT else f"L_{exit_e:04X}"
    head_lbl = f"L_{h:04X}"
    res = []
    for addr, ind, text in out:
        if addr and block_of(addr) in loop:
            t = text.strip()
            for lbl, kw in ((exit_lbl, 'break'), (head_lbl, 'continue')):
                if lbl is None:
                    continue
                if t == f"goto {lbl};":
                    text = f"{kw};"
                    break
                m = re.match(rf'if \((.+)\) goto {re.escape(lbl)};$', t)
                if m:
                    text = f"if ({m.group(1)}) {kw};"
                    break
        res.append((addr, ind, text))
    return res


def _fold_one_loop(lines, cfg, leaders, bes, loop, h):
    """Fold the single loop with header `h` (its back edges = the (u, h) in `bes`, its body
    = `loop`) with the shape-appropriate folder. The per-shape dispatch, factored out so it
    serves BOTH the lone-loop path and each disjoint SIBLING (Phase 1c). Returns the
    (possibly unchanged) line list.
      * multi-back-edge  -> top-test `while`-with-continues, or `while(1){switch}`
      * top-test         -> `while (C) { body }`         (header is the 2-way exit test)
      * bottom-test/both -> `do { body } while (C);`     (tail is the 2-way test)
    A two-test ("both") loop is a do-while whose HEADER also carries an exit-test that the
    `_emit_break_continue` post-pass rewrites to `if (C) break;`. Unmatched shapes fall back
    to the infinite-loop `while(1)`; the CFG gate is the backstop for all of them."""
    h_bes = [(u, hh) for u, hh in bes if hh == h]
    if len(h_bes) >= 2:                          # multiple jump-back points -> continues
        if len(cfg[h]) == 2:
            return _fold_while(lines, cfg, leaders, loop, None, h, multi=True)
        return _fold_while1(lines, cfg, leaders, loop, h)   # switch-header while(1)
    (u, _h), = h_bes
    htest, utest = len(cfg[h]) == 2, len(cfg[u]) == 2
    out = lines
    if u != h and htest and not utest:          # top-test  while (C) { body }
        out = _fold_while(lines, cfg, leaders, loop, u, h)
    elif (u == h and htest) or (utest and not htest):   # bottom-test do { } while (C);
        out = _fold_dowhile(lines, cfg, leaders, loop, u, h)
    elif utest and htest and u != h:            # two-test "both" loop -> do-while + hdr break
        out = _fold_dowhile(lines, cfg, leaders, loop, u, h)
    if out is lines:                            # no clean test header -> infinite-loop shape
        out = _fold_while1(lines, cfg, leaders, loop, h)
    return out


def _fold_siblings(lines, cfg, leaders, bes):
    """Phase 1c — SIBLING loops (>1 loop header whose natural loops are pairwise DISJOINT:
    sequential, not nested). Fold each independently with `_fold_one_loop`, highest header
    address first so a fold's inserted scaffold never shifts a not-yet-folded sibling's line
    indices. Each fold is GATED against the raw witness on its own, so one bad sibling fold
    can't sink the others (and the sub keeps every good one). NESTED / overlapping loops
    (a loop inside another) need inner-then-outer folding — still deferred (the big bucket)."""
    from emu_harness.vm.koei.nes import vm_cfg
    by_header = {}
    for u, h in bes:
        by_header.setdefault(h, set()).update(vm_cfg.natural_loop(u, h, cfg))
    loops = sorted(by_header.items(), key=lambda x: -x[0])   # high header addr first
    for i in range(len(loops)):
        for j in range(i + 1, len(loops)):
            if loops[i][1] & loops[j][1]:
                return lines                    # nested / overlapping -> deferred
    raw, out = lines, lines
    for h, loop in loops:
        cand = _fold_one_loop(out, cfg, leaders, bes, loop, h)
        if cand is out:
            continue
        ok, _r, _s = vm_cfg.structured_equivalent(raw, cand, leaders)
        if ok:
            out = cand                          # keep only CFG-preserving sibling folds
    return out


def structure_loops(lines, instructions):
    """CFG epic, Phase 1: fold reducible loops to `while` / `do-while` / `while(1)`.
    CFG-DRIVEN — header / body / exit come from the bytecode CFG (ground truth), never
    from line-text guessing. A sub with ONE loop header folds that loop (`_fold_one_loop`);
    a sub with several DISJOINT headers folds each sibling (`_fold_siblings`, Phase 1c).
    Nested loops (a header inside another's body) are still deferred. The CFG-equivalence
    gate is the backstop — a mis-fold reverts (per-sibling, or the whole sub) to honest goto."""
    from emu_harness.vm.koei.nes import vm_cfg
    cfg, leaders = vm_cfg.bytecode_cfg(instructions)
    if len(leaders) < 2:
        return lines
    bes, _dom = vm_cfg.back_edges(cfg, leaders[0])
    if not bes:
        return lines
    headers = {hh for _u, hh in bes}
    if len(headers) != 1:
        return _fold_siblings(lines, cfg, leaders, bes)
    h = next(iter(headers))
    loop = set()
    for ub, _h in bes:
        loop |= vm_cfg.natural_loop(ub, h, cfg)
    return _fold_one_loop(lines, cfg, leaders, bes, loop, h)


def _fold_while(lines, cfg, leaders, loop, u, h, multi=False):
    """Top-test loop -> `while (C) { body }`. Handles the common ROTATED idiom (test
    emitted at the BOTTOM with an entry `goto L_test` guard, body above) by hoisting
    the test to the top — lower_struct_cfg's body-leaders redirect keeps the
    address-based raw lowering correct after the hoist.

    multi=True (Phase 2): the loop has SEVERAL back edges to this one header. Only the
    structural tail (the back-goto that is the very last loop line) is dropped into the
    `while`; every OTHER in-body `goto L_h` is left in place for `_emit_break_continue`
    to rewrite as `continue;`. `u` is unused (kept for call symmetry)."""
    from emu_harness.vm.koei.nes import vm_cfg
    body_in = [s for s in cfg[h] if s in loop]
    exit_out = [s for s in cfg[h] if s not in loop]
    if len(body_in) != 1 or len(exit_out) != 1:
        return lines
    body_entry, exit_e = body_in[0], exit_out[0]
    if body_entry == h:                     # header is its own body entry: skip
        return lines
    if not _loop_single_entry(cfg, loop, h, exit_e):
        return lines

    block_of = vm_cfg._block_of_fn(leaders)
    idx_h_test = idx_backedge = idx_entry_guard = None
    loop_idxs = []
    for i, (addr, _ind, text) in enumerate(lines):
        blk = block_of(addr)
        kind, tgt, _cond = _parse_branch_line(text)
        if blk in loop:
            loop_idxs.append(i)
            if blk == h and kind == 'ifgoto':
                idx_h_test = i                       # header test (removed; gives C)
            elif kind == 'goto' and tgt == h and blk != h:
                idx_backedge = i                     # explicit tail back-goto (removed)
        elif kind == 'goto' and tgt == h:
            idx_entry_guard = i                      # guard from outside (removed)
    if idx_h_test is None or not loop_idxs:
        return lines
    if multi:
        idx_backedge = None    # keep every in-body `goto L_h` -> `continue` (post-pass)
    lo, hi = min(loop_idxs), max(loop_idxs)
    # Foreign lines inside the loop's listing span are OK only if they form pure-exit
    # blocks (a `return`/`break`). Any non-exit foreign code = a genuinely interleaved
    # body -> bail (the gate is the backstop anyway). Two kinds of pure-exit interleave,
    # handled differently:
    #   * BEFORE the body entry — the classic TOP-test header's fall-through `return`
    #     emitted between the test and the body. This is the loop's EXIT, so it must be
    #     RELOCATED past the `}` (leaving it in the body would run it first iteration).
    #   * mid-body (>= body entry) — an early `return`/`break`. It STAYS in place (a
    #     mid-loop exit reads correctly there; structure_lines folds it to early-return).
    # Address-keyed lowering makes either choice CFG-neutral; this is purely readability.
    interleaved = [i for i in range(lo, hi + 1)
                   if block_of(lines[i][0]) not in loop]
    if any(not _is_pure_exit_block(block_of(lines[i][0]), cfg, loop)
           for i in interleaved):
        return lines
    be_idx = next((i for i in range(lo, hi + 1) if block_of(lines[i][0]) == body_entry), lo)
    relocate = {i for i in interleaved if i < be_idx}

    _k, tgt, cond = _parse_branch_line(lines[idx_h_test][2])
    cont = cond if block_of(tgt) in loop else _negate_cond(cond)

    drop = {idx_h_test}
    if idx_backedge is not None:
        drop.add(idx_backedge)
    if idx_entry_guard is not None:
        drop.add(idx_entry_guard)
    for i in range(lo, hi + 1):
        if lines[i][2].strip() == f"L_{h:04X}:":
            drop.add(i)                              # header label -> replaced by `while`
    # The body-entry label is now unreferenced (the header test that targeted it was
    # dropped) UNLESS an internal back-jump still uses it — drop it only if dead.
    refs = set()
    for j, (_a, _ind, text) in enumerate(lines):
        if j not in drop:
            refs.update(int(m, 16) for m in re.findall(r'goto L_([0-9A-Fa-f]{4})', text))
    if body_entry not in refs:
        for i in range(lo, hi + 1):
            if lines[i][2].strip() == f"L_{body_entry:04X}:":
                drop.add(i)

    base_ind = lines[lo][1] or 1
    out, moved = [], []
    for i, (addr, ind, text) in enumerate(lines):
        if i == lo:
            out.append((h, base_ind, f"while ({cont}) {{"))
        if i in relocate:
            moved.append((addr, base_ind, text))     # pure-exit block -> after the loop
        elif i not in drop and lo <= i <= hi:
            out.append((addr, ind + 1, text))
        elif i not in drop:
            out.append((addr, ind, text))
        if i == hi:
            out.append((0, base_ind, "}"))
            out.extend(moved)                        # exit code, now lexically after `}`
    return _emit_break_continue(out, leaders, loop, h, exit_e)


def _fold_dowhile(lines, cfg, leaders, loop, u, h):
    """Bottom-test loop -> `do { body } while (C);`. The tail block `u` ends in the
    conditional back-branch `if (C) goto L_h` (taken = continue), else falls to the
    exit. NO hoist — the test stays where it is, so address order is preserved and the
    only edits are `L_h:` -> `do {` and the tail test -> `} while (C);`. Covers both
    self-loops (u == h) and multi-block bottom-test loops (h < u)."""
    from emu_harness.vm.koei.nes import vm_cfg
    succ_u = cfg[u]
    if h not in succ_u or len(succ_u) != 2:
        return lines
    exit_e = next(s for s in succ_u if s != h)
    if not _loop_single_entry(cfg, loop, h, exit_e):
        return lines

    block_of = vm_cfg._block_of_fn(leaders)
    idx_tail = None                          # the `if (C) goto L_h` tail-test line
    loop_idxs = []
    for i, (addr, _ind, text) in enumerate(lines):
        blk = block_of(addr)
        if blk in loop:
            loop_idxs.append(i)
            kind, tgt, _cond = _parse_branch_line(text)
            if blk == u and kind == 'ifgoto' and block_of(tgt) == h:
                idx_tail = i
    if idx_tail is None or not loop_idxs:
        return lines
    lo, hi = min(loop_idxs), max(loop_idxs)
    if hi != idx_tail:                       # the test must be the LAST loop line
        return lines
    # Interleaved non-loop lines inside the do-body span — classify each:
    #  * pre-loop FLUSHED call (addr < header h, block not in loop, non-leader): a
    #    discarded-return CALL the decompiler emitted AFTER `L_h:` but tagged with its
    #    earlier, pre-header address (e.g. `read_frame_timer(1)` atop a button-wait
    #    loop like prompt_yes_no $822A / ui_helper_d3a7). It belongs BEFORE the loop and
    #    runs ONCE -> HOIST it out to before `do {`. This is the SAFE relocation direction
    #    (to an address-CONSISTENT slot, below h, beside the other pre-loop lines) — the
    #    opposite of the $8C88 move-to-a-LATER-slot that broke if/else merges. Control
    #    lines keep their address position, so an in-span line with addr < h is always a
    #    flushed side-effect; bail defensively if it looks control-shaped.
    #  * pure-exit block: leave in place (rendered inside, reached by a backward goto).
    #  * anything else: genuinely foreign control -> bail (gate is the final backstop).
    hoist = set()
    for i in range(lo, hi + 1):
        a = lines[i][0]
        if not a or block_of(a) in loop:
            continue
        t = lines[i][2].strip()
        if a < h and a not in leaders and not re.search(r'\b(goto|if|while|do|return)\b|^[}{]|:$', t):
            hoist.add(i)
        elif _is_pure_exit_block(block_of(a), cfg, loop):
            continue
        else:
            return lines

    tail_addr, _ti, tail_text = lines[idx_tail]
    _k, _tgt, cond = _parse_branch_line(tail_text)   # taken -> h is continue => while(cond)
    drop = {idx_tail} | hoist
    for i in range(lo, hi + 1):
        if lines[i][2].strip() == f"L_{h:04X}:":
            drop.add(i)                      # header label -> replaced by `do {`

    base_ind = lines[lo][1] or 1
    out = []
    for i, (addr, ind, text) in enumerate(lines):
        if i == lo:
            for hj in sorted(hoist):         # pre-loop flushed calls, hoisted before `do {`
                out.append((lines[hj][0], base_ind, lines[hj][2]))
            out.append((h, base_ind, "do {"))
        if i not in drop and lo <= i <= hi:
            out.append((addr, ind + 1, text))
        elif i not in drop:
            out.append((addr, ind, text))
        if i == hi:
            out.append((tail_addr, base_ind, f"}} while ({cond});"))
    return _emit_break_continue(out, leaders, loop, h, exit_e)


def _fold_while1(lines, cfg, leaders, loop, h):
    """Infinite-loop fold -> `while (1) { body }`. For a loop whose header IS the loop
    top with no clean 2-way condition exit (e.g. a `switch (poll_input())` menu loop):
    control leaves only via break/return inside the body. Wraps the loop's listing span
    in `while (1) { }` (the header switch + label STAY inside as the loop top); body
    `goto L_h` -> continue, `goto L_exit` -> break. Switch CASE gotos keep their labels
    (Phase 3 inlines case bodies). Requires the loop top to be the FIRST line of the
    span (no rotation) and any interleaved foreign block to be pure-exit (a mid-body
    return/break that reads correctly in place)."""
    from emu_harness.vm.koei.nes import vm_cfg
    block_of = vm_cfg._block_of_fn(leaders)
    if not _loop_single_entry(cfg, loop, h, None):
        return lines
    # A CONDITIONAL back edge (`if (C) goto L_h`: branch back to the header OR fall
    # elsewhere) is do-while / two-test territory. As while(1) the tail becomes
    # `if (C) continue;` whose FALL-THROUGH then runs — SAFE iff that fall-through EXITS:
    # the non-continue branch is a PURE-EXIT block (a `return`/`break`), so
    # `if (C) continue; return …;` exits correctly. If instead it re-enters LIVE loop
    # code, the do-while's exit would wrongly LOOP here (the gate-hole fix's concern) ->
    # bail. A plain bottom-test do-while is already intercepted by _fold_dowhile before
    # reaching here; this relaxed path lights up for the test-in-the-MIDDLE menu loop
    # (switch header, case bodies laid out AFTER the test) that do-while can't express.
    # The gate — proven sound vs the bad while(1) by test-vm-cfg $830B — is the backstop.
    for n in loop:
        if h in cfg[n] and len(cfg[n]) == 2:
            for s in cfg[n]:
                if (s is not vm_cfg.EXIT and s != h and s not in loop
                        and not _is_pure_exit_block(s, cfg, loop)):
                    return lines
    loop_idxs = [i for i, (a, _i, _t) in enumerate(lines) if block_of(a) in loop]
    if not loop_idxs:
        return lines
    lo, hi = min(loop_idxs), max(loop_idxs)
    if block_of(lines[lo][0]) != h:           # loop top must open the span (no hoist)
        return lines
    if any(not _is_pure_exit_block(block_of(lines[i][0]), cfg, loop)
           for i in range(lo, hi + 1) if block_of(lines[i][0]) not in loop):
        return lines
    # exit_e for break emission: the block right after the loop if it's an exit target,
    # else the single exit (if exactly one), else leave breaks as gotos (EXIT sentinel).
    exits = {s for n in loop for s in cfg[n] if s is not vm_cfg.EXIT and s not in loop}
    after = block_of(lines[hi + 1][0]) if hi + 1 < len(lines) else None
    exit_e = after if after in exits else (next(iter(exits)) if len(exits) == 1 else vm_cfg.EXIT)

    base_ind = lines[lo][1] or 1
    out = []
    for i, (addr, ind, text) in enumerate(lines):
        if i == lo:
            out.append((h, base_ind, "while (1) {"))
        out.append((addr, ind + 1, text) if lo <= i <= hi else (addr, ind, text))
        if i == hi:
            out.append((0, base_ind, "}"))
    return _emit_break_continue(out, leaders, loop, h, exit_e)


def structure_switches(lines, instructions):
    """CFG epic, Phase 3: INLINE switch case bodies into the braces.

    Today a switch renders as `switch(E){ case N: goto L_T; ... }` with the bodies laid out
    AFTER the closing brace. This moves each case body INSIDE the braces as a native C
    `switch` (the VM's switch is first-class — `$D5`/`$D9`). For every FOLDABLE switch
    (single-entry dispatch — see vm_cfg.switch_dispatches, the shared detector the gate also
    uses) we:
      * keep the `switch (E) {` opener, DROP the `case N: goto L_T;` dispatch lines + closer;
      * walk the body lines in ADDRESS order (never reordered, so lexical == address order is
        preserved -> the address-based gate stays faithful AND the emitted C is correct),
        inserting `case N:` / `default:` labels before each case-target block; and
      * move the closing `}` to just past the highest enclosed block.
    Internal control (goto/return) is kept verbatim — a case body whose edge can't fall
    through keeps an HONEST internal goto (Chris's option 1: formalize every single-entry
    switch, bodies inside the braces, gotos local to the case). The merge stays inside when
    it is address-interleaved with the cases (reached by goto); only a cleanly-trailing merge
    could become `break` (left for a follow-up). The CFG gate is the backstop: a fold that is
    not CFG-preserving reverts the whole sub to honest goto.
    """
    from emu_harness.vm.koei.nes import vm_cfg
    dispatches = {d['switch_addr']: d for d in vm_cfg.switch_dispatches(instructions)}
    if not dispatches:
        return lines

    def _depth_delta(t):
        return (1 if t.endswith('{') else 0) - (1 if t.startswith('}') else 0)

    def _lex_foldable(d):
        """A switch is lexically foldable only if every case target sits at brace-depth 0
        within the switch body — i.e. NOT nested inside an if/while/do block that
        structure_lines already folded around it (a `case:` jumping into a nested block is
        legal-but-ugly Duff's-device C; we leave those as honest goto-cases). Also requires
        the enclosed body to be brace-balanced so the wrap is well-formed."""
        sa, targets, body_end = d['switch_addr'], {t for _k, t in d['key_targets']}, d['body_end']
        # locate the switch opener, then skip the dispatch `case: goto` lines + closer `}`.
        j = 0
        while j < len(lines) and not (lines[j][0] == sa
                                      and re.match(r'switch \(.+\) \{$', lines[j][2].strip())):
            j += 1
        if j >= len(lines):
            return False
        j += 1
        while j < len(lines):
            inner = lines[j][2].strip()
            if lines[j][0] == sa and (vm_cfg._RE_SWITCH_CASE.match(inner) or inner == '}'):
                closed = inner == '}'
                j += 1
                if closed:
                    break
                continue
            return False
        depth = 0
        while j < len(lines):
            baddr, _bi, btext = lines[j]
            if baddr != 0 and body_end is not None and baddr >= body_end:
                break
            if baddr in targets and depth != 0:
                return False
            depth += _depth_delta(btext.strip())
            j += 1
        return depth == 0

    dispatches = {sa: d for sa, d in dispatches.items() if _lex_foldable(d)}
    if not dispatches:
        return lines

    # Per switch: target block -> the case-label lines to emit before it (key order).
    def _labels_for(d):
        by_target = {}
        for key, tgt in d['key_targets']:
            by_target.setdefault(tgt, []).append('default:' if key is None else f'case {key}:')
        return by_target

    case_target_addrs = set()      # leaders we attach a `case`/`default:` label to
    out = []
    i = 0
    n = len(lines)
    while i < n:
        addr, ind, text = lines[i]
        t = text.strip()
        d = dispatches.get(addr) if re.match(r'switch \(.+\) \{$', t) else None
        if d is None:
            out.append(lines[i])
            i += 1
            continue
        # Foldable switch opener. Emit it, then SKIP the dispatch case lines + the closer `}`
        # (all share the switch instruction's addr and sit immediately after the opener).
        out.append((addr, ind, text))
        i += 1
        while i < n:
            inner = lines[i][2].strip()
            if lines[i][0] == addr and (vm_cfg._RE_SWITCH_CASE.match(inner) or inner == '}'):
                closed = inner == '}'
                i += 1
                if closed:
                    break
                continue
            break
        # Now inline the body lines: addr in (switch_addr, brace_close]. Insert case labels
        # at the first line of each target block; +1 indent everything; close after the last.
        by_target = _labels_for(d)
        labeled = set()
        body_end = d['body_end']
        while i < n:
            baddr, bind, btext = lines[i]
            # Stop the switch body at the first line of the block PAST the territory (the
            # `body_end` leader). None => the territory runs to the function end. Inserted
            # scaffold carries addr 0 — keep consuming it as part of the current body.
            if baddr != 0 and body_end is not None and baddr >= body_end:
                break
            if baddr in by_target and baddr not in labeled:
                for lab in by_target[baddr]:
                    out.append((baddr, ind, lab))
                labeled.add(baddr)
                case_target_addrs.add(baddr)
            out.append((baddr, bind + 1, btext))
            i += 1
        # A case targeting the trailing merge M (== body_end, the block right after the `}`)
        # has no block inside the territory — emit it as an EMPTY trailing case that falls
        # through the `}` to M (fall-off-the-switch == break semantics; the address-based gate
        # models it as a fall to body_end). Covers the fall-through ladders (e.g. $9BB4:
        # `terrain = 3 - count`, where `default` points at the post-decrement return).
        for tgt in sorted(t for t in by_target if t not in labeled):
            if tgt == body_end:
                for lab in by_target[tgt]:
                    out.append((tgt, ind, lab))
                labeled.add(tgt)
                case_target_addrs.add(tgt)
        out.append((0, ind, "}"))

    # Drop the now-redundant `L_XXXX:` labels at case-target leaders: their only references
    # were the `case N: goto L_X;` dispatch lines we removed. A label still targeted by a
    # surviving `goto` (e.g. a shared merge inside a case body) is kept. Labels are scaffold,
    # so removing an unreferenced one is CFG-neutral (the gate is the backstop).
    referenced = set()
    for _a, _ind, txt in out:
        for m in re.finditer(r'goto L_([0-9A-Fa-f]{4})', txt):
            referenced.add(int(m.group(1), 16))
    pruned = []
    for a, ind, txt in out:
        lm = re.match(r'L_([0-9A-Fa-f]{4}):$', txt.strip())
        if lm and a in case_target_addrs and int(lm.group(1), 16) not in referenced:
            continue
        pruned.append((a, ind, txt))
    return pruned


def structure_lines(lines, case_barriers=frozenset()):
    """Recognize simple if-then-else patterns and convert goto → structured C.

    lines: list of (addr, indent, text) tuples.
    case_barriers: switch case-target addresses (Phase 3). A braced if/if-else fold whose
      body would ENCLOSE one is suppressed — a case entry nested inside an if-block is
      legal-but-ugly Duff's-device C and, worse, blocks structure_switches from inlining the
      switch (its lexical-foldability guard needs case targets at brace-depth 0). Keeping the
      goto there lets the switch fold cleanly instead; the local if just stays a goto.
    Returns: same shape, with if/else/closing-brace inserted where applicable.
    """
    # Index lines by what label they emit (e.g., "L_87FD:") and by address.
    out = []
    label_to_pos = {}
    for i, (addr, ind, text) in enumerate(lines):
        m = re.match(r'L_([0-9A-Fa-f]{4}):', text.strip())
        if m:
            label_to_pos[m.group(1).upper()] = i

    # First pre-pass: recognize "early return" idiom.
    #   if (cond) goto L_X;     <- if (cond) goto L_X then fall-through is the "else"
    #   <fall-through body — typically a single return>
    # L_X:
    #   <main body>
    # → Replace with: if (!cond) <fall-through body>;  remove L_X label.
    cleaned = []
    i = 0
    while i < len(lines):
        addr, ind, text = lines[i]
        m = re.match(r'if \((.+)\) goto L_([0-9A-Fa-f]{4});', text.strip())
        if m and i + 2 < len(lines):
            cond = m.group(1)
            tgt = m.group(2).upper()
            # Check if next line is a simple return, and the line after is the label
            next_text = lines[i + 1][2].strip()
            label_text = lines[i + 2][2].strip()
            ret_match = re.match(r'return (.+);$', next_text)
            label_match = re.match(rf'L_{tgt}:$', label_text)
            if ret_match and label_match:
                # Early-return idiom found
                neg_cond = invert_condition(cond)
                cleaned.append((addr, ind, f"if ({neg_cond}) return {ret_match.group(1)};"))
                i += 3  # skip the goto, return, and label
                continue
        cleaned.append(lines[i])
        i += 1
    lines = cleaned

    # Rebuild label index after cleaning
    label_to_pos = {}
    for i, (addr, ind, text) in enumerate(lines):
        m = re.match(r'L_([0-9A-Fa-f]{4}):', text.strip())
        if m:
            label_to_pos[m.group(1).upper()] = i

    i = 0
    indent_stack = [1]  # current indent depth
    pending_close = []  # list of (at_pos, indent_to_drop_to) -- emit '}' before this line
    while i < len(lines):
        addr, ind, text = lines[i]

        # Check if we should emit a closing brace before this line
        while pending_close and pending_close[-1][0] == i:
            close_indent = pending_close.pop()[1]
            out.append((0, close_indent, "}"))

        # Pattern: "if (!(cond)) goto L_X;" — try to convert to "if (cond) {" structure
        m = re.match(r'if \(!\((.+)\)\) goto L_([0-9A-Fa-f]{4});', text.strip())
        if m and m.group(2).upper() in label_to_pos:
            cond = m.group(1)
            tgt_pos = label_to_pos[m.group(2).upper()]
            # Only convert if target is FORWARD (positive direction) and there's no
            # branch ESCAPING the range [i+1, tgt_pos)
            if tgt_pos > i:
                # Check escape branches within [i+1, tgt_pos-1]
                escapes = False
                last_in_block = lines[tgt_pos - 1]
                else_target = None
                for j in range(i + 1, tgt_pos):
                    txt = lines[j][2]
                    bm = re.search(r'goto L_([0-9A-Fa-f]{4})', txt)
                    if bm:
                        tgt2 = bm.group(1).upper()
                        if label_to_pos.get(tgt2, -1) > tgt_pos:
                            # Forward goto past the if-end → potential else
                            if j == tgt_pos - 1 and 'if' not in txt:
                                # Last line is unconditional goto: it's an if/else
                                else_target = tgt2
                            else:
                                escapes = True
                                break
                        elif label_to_pos.get(tgt2, -1) < i:
                            # Backward goto → loop or complex flow
                            escapes = True
                            break
                # Phase 3: don't let the (if/if-else) body ENCLOSE a switch case entry — the
                # fold region is [i+1, fold_end); a case target inside it would nest the case.
                if not escapes and case_barriers:
                    fold_end = label_to_pos[else_target] if else_target else tgt_pos
                    if any(lines[k][0] in case_barriers for k in range(i + 1, fold_end)):
                        escapes = True
                if not escapes:
                    out.append((addr, ind, f"if ({cond}) {{"))
                    indent_stack.append(ind + 1)
                    if else_target:
                        # Close at end of then-body, open else at L_X, close else at else_target
                        else_pos = label_to_pos[else_target]
                        pending_close.append((tgt_pos, ind))  # close then-block at L_X
                        # We'll inject "} else {" at that label
                        # For now, just mark it for the label-emitter below
                        # Strip the trailing "goto L_else_target;" from the last line of then-block
                        i += 1
                        # Emit body of then-block minus the trailing goto
                        for k in range(i, tgt_pos - 1):
                            out.append(lines[k])
                        # Mark: when we hit tgt_pos (L_X label), emit "} else {"
                        # Then close at else_pos.
                        # Quick hack: just emit close, the label, then "else {"
                        out.append((0, ind, "} else {"))
                        for k in range(tgt_pos, else_pos):
                            # Skip the label line itself
                            if k == tgt_pos and re.match(r'L_', lines[k][2].strip()):
                                continue
                            out.append(lines[k])
                        out.append((0, ind, "}"))
                        i = else_pos
                        continue
                    else:
                        pending_close.append((tgt_pos, ind))
                        i += 1
                        continue

        out.append((addr, ind, text))
        i += 1

    # Emit any trailing closing braces
    while pending_close:
        close_indent = pending_close.pop()[1]
        out.append((0, close_indent, "}"))

    return out


# Opcodes whose decompiler model legitimately diverges from the runtime data-stack
# delta and so are NOT gated by the drift-guard (documented, not silently skipped).
# Populated empirically from the first clean run; each entry says WHY the decompiler's
# observed Δ differs from STACK_EFFECT (it's an abstraction, not a faithful VM stack).
_SELF_CHECK_EXCLUDE = {
    # (none yet — added with a reason if the first run surfaces a benign divergence)
}


def check_stack_trace():
    """Compare the SELF_CHECK trace (observed data-stack Δ per opcode, from the REAL
    handlers) against the audited `vm_stack_effect.STACK_EFFECT`. Returns
    (mismatches, observed) where mismatches is a list of (op, expected, got, mnem).

    Only GATED classes (data/push/pop/store) are checked — their data-stack Δ is
    branch-independent and must equal pushes-pops. call/return/control/probe/ext are
    skipped (the decompiler models calls via variable arity / registers, not a fixed
    data-stack Δ). See _SELF_CHECK_EXCLUDE for documented per-opcode carve-outs."""
    from collections import defaultdict
    from emu_harness.vm.koei.nes.vm_stack_effect import STACK_EFFECT, GATED_CLASSES
    observed = defaultdict(set)
    for i in range(len(_STACK_TRACE) - 1):
        op, depth = _STACK_TRACE[i]
        if op is None:                       # terminal marker — don't pair across subs
            continue
        observed[op].add(_STACK_TRACE[i + 1][1] - depth)
    mismatches = []
    for op, deltas in sorted(observed.items()):
        if op in _SELF_CHECK_EXCLUDE:
            continue
        eff = STACK_EFFECT.get(op)
        if eff is None or eff.cls not in GATED_CLASSES:
            continue
        expected = eff.pushes - eff.pops    # net words onto the data stack
        if deltas != {expected}:
            mismatches.append((op, expected, sorted(deltas), eff.mnem))
    return mismatches, observed


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    filepath = sys.argv[1]
    sub_addr = int(sys.argv[2], 16)
    # Load mesen labels so standalone runs name functions like the batch driver.
    labels = None
    try:
        from emu_harness.vm.koei.nes.nobunaga_vm import load_labels
        labels = load_labels()
    except Exception:
        pass
    # Var-walk names: bank inferred from the listing filename (bank_NN_vm.asm).
    var_names = None
    try:
        from emu_harness.vm.koei.nes.titles import game_cfg      # NA1's labels (the standalone default)
        toml = Path(game_cfg("na1")["_labels"])
        bm = re.search(r'bank_(\d+)', filepath)
        if bm:
            var_names = load_var_names(toml).get((int(bm.group(1)), sub_addr))
    except Exception:
        pass
    decompile(filepath, sub_addr, labels, var_names=var_names)


if __name__ == "__main__":
    main()
