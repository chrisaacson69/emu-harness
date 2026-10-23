"""
tools/vm_cfg.py — Control-flow-graph extractor + C-line-list lowering.

CFG epic, Phase 0 (the honesty gate that licenses every structuring fold).

Two CFGs over ONE key space. The key space is BYTECODE ADDRESSES, because the
decompiler renders every branch target as a label `L_XXXX:` whose XXXX *is* the
bytecode target address (vm_decompile.decompile), and every `goto L_XXXX` /
`case N: goto L_XXXX` references that same address. So the C control flow and the
bytecode control flow live in the same coordinate system — no name-matching, no
synthetic labels, no fuzzy alignment.

  bytecode_cfg(instructions)      -> (cfg, leaders)
      The GROUND TRUTH basic-block graph, built straight from the decoded
      bytecode. cfg = {leader_addr: frozenset(succ)}; succ entries are leader
      addrs or the EXIT sentinel. leaders = sorted block-start addresses.

  lower_c_to_cfg(lines, leaders)  -> cfg
      Lowers a (addr, indent, text) C line list — the RAW goto witness OR the
      STRUCTURED if/while/switch form — to the SAME block graph, by interpreting
      its control flow against the shared `leaders` partition.

The equivalence gate (tools/vm-cfg-gate.py) asserts lower_c_to_cfg(...) == cfg for
every sub: a structuring transform is honest iff it preserves the CFG. Where a
transform can't be proven CFG-preserving, the decompiler keeps an honest `goto`
(which lowers trivially), so fidelity rises monotonically and is never wrong.

Granularity note — why this is robust to expression folding: most VM instructions
(loads, immediates, register ops) emit NO C line; they fold into the next
committing statement's expression. So C statement addresses are a SUBSET of
bytecode instruction addresses. We never compare per-instruction; we collapse both
sides to the basic-block partition via block_of(addr) (= largest leader <= addr).
A basic block that emits NO statement at all (a branch target whose every
instruction folds, then falls through) has exactly one honest C reading —
fall through to the next block — which is reconstructed from `leaders` alone (a
folded block cannot branch, because a branch always emits a statement).
"""
import bisect
import re

EXIT = "EXIT"   # sentinel successor: control leaves the sub (return / fall off end)

# UN-SHARING (composition fix): the compiler SHARES a block that several constructs converge on
# (a terminal tail, or a loop's continue-prologue/increment). To recover the nesting we must put the
# lost boundary back — DUPLICATE the shared block per predecessor. A copy is emitted at a SYNTHETIC
# address that encodes which real block it copies: synth = _SYNTH_BASE + (original << 8) + idx. The
# struct lowering self-augments its leader set with synth addrs (each copy is its own block); the
# gate bypasses each synth copy AND its decoded original on both sides (a known duplicate routes the
# same as the shared original), and the copies==in-edges guard (vm_reduce) covers content. This is
# the general form of atom 5 (terminal sink, cone=∅) extended to bounded non-terminal cones.
_SYNTH_BASE = 0x100000


def _synth_origin(addr):
    """The real block a synthetic copy was made from (None if `addr` isn't synthetic)."""
    return ((addr - _SYNTH_BASE) >> 8) if addr >= _SYNTH_BASE else None


# --- branch/switch/return recognition on the decoded bytecode -----------------
# Mirrors the control opcodes vm_decompile.decompile() emits: branch_z/nz_abs ->
# conditional, jump_abs -> unconditional, switch -> computed (case+default targets),
# vm_return -> EXIT. Everything else falls through to addr+len.
_COND_BRANCH = frozenset({'branch_z_abs', 'branch_nz_abs'})
_UNCOND_JUMP = frozenset({'jump_abs'})


def _target(ins):
    """The $XXXX absolute target in a branch/jump operand, or None."""
    m = re.search(r'\$([0-9A-Fa-f]{4})', ins['operand'] or '')
    return int(m.group(1), 16) if m else None


def _switch_targets(ins):
    """(case+default target addrs, has_default) decoded from a switch's comment,
    where vm-disasm wrote `<key>=>$<tgt> ... default=>$<tgt>`."""
    tgts = {int(t, 16) for t in re.findall(r'=>\$([0-9A-Fa-f]{4})', ins['comment'] or '')}
    has_default = 'default=>$' in (ins['comment'] or '')
    return tgts, has_default


def _instr_succ(instructions):
    """Per-instruction successor sets keyed by addr (entries: addr or EXIT), plus
    the diagnostic list of any unrecognised 'branch'/'jump' mnemonics encountered."""
    addrs = [ins['addr'] for ins in instructions]
    addr_set = set(addrs)
    by_addr = {ins['addr']: ins for ins in instructions}
    unknown = []

    def fallthrough(ins):
        nxt = ins['addr'] + len(ins['bytes'])
        return nxt if nxt in addr_set else EXIT

    succ = {}
    for ins in instructions:
        a, mn = ins['addr'], ins['mnemonic']
        if mn == 'vm_return':
            succ[a] = {EXIT}
        elif mn in _UNCOND_JUMP:
            t = _target(ins)
            succ[a] = {t if t is not None else EXIT}
        elif mn in _COND_BRANCH:
            t = _target(ins)
            succ[a] = {t if t is not None else EXIT, fallthrough(ins)}
        elif mn == 'switch':
            tgts, has_default = _switch_targets(ins)
            s = set(tgts)
            if not has_default:
                s.add(fallthrough(ins))   # unmatched selector falls past the switch
            succ[a] = s or {fallthrough(ins)}
        else:
            # Defensive: surface any other control-shaped mnemonic instead of silently
            # mismodelling it (the gate then fails loudly rather than passing a lie).
            if 'branch' in mn:
                t = _target(ins)
                succ[a] = {t if t is not None else EXIT, fallthrough(ins)}
                unknown.append((a, mn))
            elif 'jump' in mn:
                t = _target(ins)
                succ[a] = {t if t is not None else EXIT}
                unknown.append((a, mn))
            else:
                succ[a] = {fallthrough(ins)}
    return succ, addrs, by_addr, unknown


def _leaders(instructions, succ):
    """Basic-block leaders: the entry, every branch/switch target, and every
    instruction that immediately follows a control transfer."""
    addr_set = {ins['addr'] for ins in instructions}
    leaders = {instructions[0]['addr']}
    for ins in instructions:
        a, mn = ins['addr'], ins['mnemonic']
        transfers = (mn == 'vm_return' or mn in _UNCOND_JUMP or mn in _COND_BRANCH
                     or mn == 'switch' or 'branch' in mn or 'jump' in mn)
        if not transfers:
            continue
        for s in succ[a]:
            if s is not EXIT and s in addr_set:
                leaders.add(s)
        nxt = a + len(ins['bytes'])
        if nxt in addr_set:
            leaders.add(nxt)          # the instruction after a transfer starts a block
    return sorted(leaders)


def _block_of_fn(leaders_sorted):
    """addr -> the leader of the block containing it (largest leader <= addr)."""
    def block_of(a):
        i = bisect.bisect_right(leaders_sorted, a) - 1
        return leaders_sorted[i] if i >= 0 else leaders_sorted[0]
    return block_of


def _bytecode_cfg_raw(instructions):
    """Ground-truth basic-block CFG, UN-contracted. Returns (cfg, leaders) where
    cfg = {leader: frozenset(succ leaders / EXIT)} and leaders is sorted."""
    if not instructions:
        return {}, []
    succ, addrs, by_addr, _unknown = _instr_succ(instructions)
    leaders = _leaders(instructions, succ)
    block_of = _block_of_fn(leaders)

    cfg = {}
    for idx, L in enumerate(leaders):
        hi = leaders[idx + 1] if idx + 1 < len(leaders) else None
        # terminator = the last instruction inside [L, hi)
        term = None
        for a in addrs:
            if a < L:
                continue
            if hi is not None and a >= hi:
                break
            term = a
        out = succ[term] if term is not None else {EXIT}
        cfg[L] = frozenset(EXIT if s is EXIT else block_of(s) for s in out)
    return cfg, leaders


def bytecode_cfg(instructions):
    """Ground-truth basic-block CFG with VALUE-DIAMONDS contracted (a value-diamond is an
    EXPRESSION — `cond ? a : b` — not control flow, exactly as C `?:` compiles to a branch
    yet is an expression). The decompiler folds these to a ternary, so the C carries no
    branch there; contracting the bytecode side keeps `lower(raw) == bytecode_cfg` valid.
    Both sides use the SAME detector (`value_diamonds`), so they can't disagree."""
    cfg, leaders = _bytecode_cfg_raw(instructions)
    diamonds = value_diamonds(instructions)
    regions = boolean_regions(instructions)
    creg = control_shortcircuits(instructions)
    if not diamonds and not regions and not creg:
        return cfg, leaders
    block_of = _block_of_fn(leaders)
    cfg = dict(cfg)
    removed = set()
    for d in diamonds:                       # 1-condition: head -> merge, drop both arms
        cfg[block_of(d['head'])] = frozenset({d['merge']})
        removed |= {d['taken_block'], d['fall_block']}
    for r in regions:                        # K-condition value short-circuit: entry -> merge
        cfg[r['entry']] = frozenset({r['merge']})
        removed |= (r['cond'] | r['value_blocks']) - {r['entry']}
    for r in creg:                           # CONTROL short-circuit `if(c1||c2…){body}`: the
        # guard chain collapses to ONE branch entry -> {body, skip}; the middle guards drop
        # (their addresses absorb into the entry block). Unlike the value cases this does NOT
        # collapse to the merge — the body still executes conditionally; only the compound
        # condition is recovered (decoder emits `if(!(c1||c2…)) goto skip`).
        cfg[r['entry']] = frozenset({r['body'], r['skip']})
        removed |= r['cond'] - {r['entry']}
    for b in removed:
        cfg.pop(b, None)
    return cfg, [L for L in leaders if L not in removed]


def _predecessors(cfg):
    preds = {}
    for L, succ in cfg.items():
        for s in succ:
            if s is not EXIT:
                preds.setdefault(s, set()).add(L)
    return preds


# Pure register/ALU mnemonics — update regA/regB ONLY, emit no statement, touch no data
# stack. A value-diamond arm may be ANY straight-line run of these, not just a bare load:
# the decompiler tracks regA SYMBOLICALLY through them (`decA` -> `(A - 1)`, `shl` -> `(A << B)`),
# so snapshotting state.regA at the arm boundary reproduces the whole expression. This is what
# lets a COMPUTED arm fold — e.g. `arg1 ? (arg1 - 1) : 2` ($D2A2), where the true arm decrements
# rather than loading a constant (the case the load-only check used to drop).
_PURE_REGB_OPS = frozenset((
    'loadB_local_pos', 'loadB_local_neg', 'loadB_imm_word', 'loadB_imm_byte',
    'loadB_imm_sbyte', 'loadB_mem_word', 'setB_imm4',
))
_PURE_REGA_OPS = frozenset((
    'loadA_local_pos', 'loadA_local_neg', 'loadA_imm_word', 'loadA_imm_byte',
    'loadA_imm_sbyte', 'loadA_mem_word', 'loadA_imm4', 'addA_imm4', 'addA_imm_sbyte',
    'incA', 'decA', 'aslA', 'lsrA', 'is_zero', 'op_CB', 'loadA_ind_byte', 'loadA_ind_word',
    'swap_AB', 'add', 'sub', 'mul', 'div_signed', 'mod_signed', 'div', 'div_unsigned',
    'mod_unsigned', 'bitand', 'bitor', 'bitxor',
    'shl_by_regB', 'shr_by_regB', 'ashr_by_regB', 'lshr_by_regB',
    'cmp_slt', 'cmp_sgt', 'cmp_sle', 'cmp_sge', 'cmp_eq', 'cmp_ne',
    'cmp_uge', 'cmp_ule', 'cmp_ugt', 'cmp_ult',
))
_PURE_VALUE_OPS = _PURE_REGA_OPS | _PURE_REGB_OPS


def _value_arm(instrs):
    """A diamond ARM block: a side-effect-free register COMPUTATION that leaves a value in
    regA (loads + pure ALU ops, e.g. `arg1 - 1` or `(col << 3)` — not just a bare load), plus
    an optional trailing unconditional `jump_abs`. Emits no statement and touches no data
    stack, so capturing state.regA at the arm boundary reproduces the full expression.
    Returns (is_arm, jump_addr_or_None)."""
    if not instrs:
        return False, None
    jump_addr = None
    body = instrs
    if instrs[-1]['mnemonic'] == 'jump_abs':
        jump_addr, body = instrs[-1]['addr'], instrs[:-1]
    if not body or not all(i['mnemonic'] in _PURE_VALUE_OPS for i in body):
        return False, None
    if not any(i['mnemonic'] in _PURE_REGA_OPS for i in body):  # arm must PRODUCE a regA value
        return False, None
    return True, jump_addr


def _arm_exit_regb(arm_instrs):
    """A signature of the arm's EXIT regB. The value-diamond fold builds `regA = cond ? a : b`
    and lets the merge run ONCE with all OTHER state shared — sound iff regA is the only thing
    that differs between the arms. If the arms leave DIFFERENT regB, the merge may consume that
    regB (the $AF46 trap: two arms load the same regA but a different `province_to_map_section`
    into regB, merged by a later `add` — folding regA-only silently drops the regB difference).
    So `value_diamonds` rejects the fold unless both arms' signatures match. None = regB
    untouched (carried in identically). `swap_AB` makes regB depend on the arm's regA, so it
    poisons the signature (unique per arm) to force rejection."""
    body = arm_instrs[:-1] if arm_instrs and arm_instrs[-1]['mnemonic'] == 'jump_abs' else arm_instrs
    sig = None
    for i in body:
        m = i['mnemonic']
        if m == 'swap_AB':
            return ('swap', i['addr'])                          # never matches the other arm
        if m in _PURE_REGB_OPS:
            sig = (m, i.get('operand', ''), tuple(i.get('bytes', ()))[:1])   # opcode byte disambiguates setB nibble
    return sig


def value_diamonds(instructions):
    """Find value-producing conditional diamonds: a 2-way head whose two arms each just
    LOAD a value into regA and converge at a single merge — i.e. `regA = cond ? vA : vB`.
    The decompiler renders these as empty `if(){}else{}` / gotos-to-empty-labels and DROPS
    the selected value (keeping only one arm). Returns a list of dicts:
      {head, polarity('z'|'nz'), taken_block, fall_block, merge, fall_jump_addr, taken_jump_addr}
    Conservative guards keep the fold sound: abs conditional head only; each arm's SOLE
    predecessor is the head (so removing it strands nothing — this excludes shared/join
    arms = compound `(X||Y)?a:b` conditions); both arms converge on one non-EXIT merge;
    and FORWARD layout `head<fall<taken<merge` (the decode loop folds in address order).
    A call-VALUED cond is fine: capturing `dia_cond = state.regA` READS regA, which the
    decompiler's getter treats as CONSUMING the pending call, so the arm's later regA write
    won't flush it as a duplicate statement — the call lives only in the ternary."""
    cfg, leaders = _bytecode_cfg_raw(instructions)
    block_of = _block_of_fn(leaders)
    bmap = {L: [] for L in leaders}
    for ins in instructions:
        bmap[block_of(ins['addr'])].append(ins)
    preds = _predecessors(cfg)
    out = []
    for H in leaders:
        succ = cfg[H]
        if len(succ) != 2 or EXIT in succ:
            continue
        hblock = bmap[H]
        if not hblock:
            continue
        branch = hblock[-1]
        if branch['mnemonic'] not in ('branch_z_abs', 'branch_nz_abs'):
            continue
        tgt = _target(branch)
        if tgt is None:
            continue
        taken = block_of(tgt)
        fall = next((s for s in succ if s != taken), None)
        if fall is None or taken == fall or taken not in succ:
            continue
        ok_t, jmp_t = _value_arm(bmap.get(taken, []))
        ok_f, jmp_f = _value_arm(bmap.get(fall, []))
        if not (ok_t and ok_f):
            continue
        # NOTE: the arms are side-effect-free `_value_arm` runs, so the ONLY mutable carried
        # state that can differ between them is regA and regB. The decompiler captures BOTH at
        # the arm boundaries and builds a ternary for whichever differs (identical values
        # collapse to a single value). This is what folds the $AF46 trap — two arms that load
        # the SAME regA but a different `province_to_map_section` into regB, merged by a later
        # `add`: the regB ternary carries the difference instead of the linear sweep dropping it.
        # (`_arm_exit_regb` previously REJECTED a differing-regB diamond; now regB is folded too.)
        if preds.get(taken) != {H} or preds.get(fall) != {H}:
            continue
        ct, cf = cfg.get(taken, frozenset()), cfg.get(fall, frozenset())
        if len(ct) != 1 or ct != cf or EXIT in ct:
            continue
        merge = next(iter(ct))
        # FORWARD layout only: head < fall-arm < taken-arm < merge. The decode loop folds
        # in address order (capture the fall value on entering the taken arm, build the
        # ternary at the merge), so a backward branch / rotated layout would mis-capture.
        if not (fall < taken < merge):
            continue
        out.append({
            'head': branch['addr'],
            'polarity': 'z' if branch['mnemonic'] == 'branch_z_abs' else 'nz',
            'taken_block': taken, 'fall_block': fall, 'merge': merge,
            'fall_jump_addr': jmp_f, 'taken_jump_addr': jmp_t,
        })
    return out


def _neg(c):
    """Negate a C boolean condition (reusing the decompiler's invert idiom inline)."""
    c = c.strip()
    if c.startswith('!(') and c.endswith(')'):
        return c[2:-1]
    return f"!({c})"


def _band(x, y):
    if x == 'F' or y == 'F': return 'F'
    if x == 'T': return y
    if y == 'T': return x
    return f"({x} && {y})"


def _bor(x, y):
    if x == 'T' or y == 'T': return 'T'
    if x == 'F': return y
    if y == 'F': return x
    return f"({x} || {y})"


def _combine_bool(c, T, F):
    """The boolean for a 2-way block: (c && reach-via-true) || (!c && reach-via-false),
    simplified for the &&/|| chain cases so a clean chain yields `c1 && c2 && …`."""
    if T == 'T' and F == 'F': return c
    if T == 'F' and F == 'T': return _neg(c)
    if F == 'F': return _band(c, T)            # c && T
    if T == 'T': return _bor(c, F)             # c || F
    if T == 'F': return _band(_neg(c), F)      # !c && F
    if F == 'T': return _bor(_neg(c), T)       # !c || T
    return _bor(_band(c, T), _band(_neg(c), F))


def boolean_regions(instructions):
    """Short-circuit boolean regions: K>=2 conditional blocks + exactly 2 value-load blocks
    converging at one merge → `regA = <boolean of the K conditions> ? vA : vB` (&&/|| chains;
    e.g. is_tile_in_bounds = (x<=10 && y<=4)). The 1-condition case is a plain value-diamond
    (`value_diamonds`); THIS handles the multi-predecessor compound conditions those leave
    behind (a value arm reached from several branch points = a short-circuit exit). Same
    soundness guards: a single region entry, every branch stays in the region, both value
    arms are reached only from inside it, and forward layout (entry = min, merge = max) so
    the decoder can fold in address order. Returns region dicts for the decoder + the gate."""
    cfg, leaders = _bytecode_cfg_raw(instructions)
    block_of = _block_of_fn(leaders)
    bmap = {L: [] for L in leaders}
    for ins in instructions:
        bmap[block_of(ins['addr'])].append(ins)
    preds = _predecessors(cfg)
    regions, used = [], set()
    for M in leaders:
        if M in used:
            continue
        varms = [p for p in preds.get(M, ()) if _value_arm(bmap.get(p, []))[0]]
        if len(varms) != 2:
            continue
        va, vb = varms
        # BFS back from the two value arms through 2-way conditional blocks only.
        cond, stack, ok = set(), [], True
        for v in (va, vb):
            stack += [p for p in preds.get(v, ()) if p not in (va, vb)]
        while stack:
            b = stack.pop()
            if b in cond:
                continue
            blk = bmap.get(b, [])
            if not blk or blk[-1]['mnemonic'] not in ('branch_z_abs', 'branch_nz_abs') or len(cfg[b]) != 2:
                ok = False
                break
            cond.add(b)
            stack += [p for p in preds.get(b, ()) if p not in cond and p not in (va, vb)]
        if not ok or len(cond) < 2:
            continue
        nodes = cond | {va, vb}
        if not all(all(s in nodes for s in cfg[c]) for c in cond):
            continue                                  # a branch escapes the region
        if any(p not in cond for p in preds.get(va, ())) or any(p not in cond for p in preds.get(vb, ())):
            continue                                  # a value arm is reached from outside
        entries = [c for c in cond if not (preds.get(c, set()) & cond)]
        if len(entries) != 1:
            continue                                  # not a single-entry region
        entry = entries[0]
        members = list(cond) + [va, vb]
        if not (entry == min(members) and M > max(members)):
            continue                                  # forward layout only
        cinfo = {}
        for c in cond:
            br = bmap[c][-1]
            tgt = block_of(_target(br))
            fall = next(s for s in cfg[c] if s != tgt)
            # branch_z (JUMPF) jumps when cond FALSE; branch_nz (JUMPT) when TRUE.
            tsucc, fsucc = (fall, tgt) if br['mnemonic'] == 'branch_z_abs' else (tgt, fall)
            cinfo[c] = {'branch_addr': br['addr'], 'true': tsucc, 'false': fsucc}
        # Of the two value blocks, exactly one JUMPS to the merge (the lower-addr one,
        # jumping over the other) and one FALLS into it — the decoder captures the jumper's
        # value at its jump and the faller's value on reaching the merge.
        jmp_a, jmp_b = _value_arm(bmap[va])[1], _value_arm(bmap[vb])[1]
        if (jmp_a is None) == (jmp_b is None):
            continue                                  # need exactly one jumper + one faller
        jumper, faller = (va, vb) if jmp_a is not None else (vb, va)
        used.add(M)
        regions.append({
            'entry': entry, 'merge': M, 'cond': cond, 'cinfo': cinfo,
            'va': va, 'vb': vb, 'value_blocks': {va, vb},
            'cond_branch_addrs': {cinfo[c]['branch_addr']: c for c in cond},
            'jumper': jumper, 'faller': faller,
            'jump_addr': _value_arm(bmap[jumper])[1],
        })
    return regions


def recover_bool_formula(region, cond_expr, target):
    """C boolean expression for reaching value block `target` from the region entry, given
    each cond block's rendered condition (cond_expr[block]). Pure recursion over the branch
    DAG with algebraic simplification (`_combine_bool`) — a clean &&/|| chain collapses to
    `c1 && c2 && …` / `c1 || c2 || …`."""
    cinfo, vblocks, entry = region['cinfo'], region['value_blocks'], region['entry']
    memo = {}

    def rec(block):
        if block == target:
            return 'T'
        if block in vblocks:
            return 'F'
        if block in memo:
            return memo[block]
        ci = cinfo[block]
        memo[block] = r = _combine_bool(cond_expr[block], rec(ci['true']), rec(ci['false']))
        return r
    return rec(entry)


def control_shortcircuits(instructions):
    """Short-circuit CONTROL regions: `if (c1 || c2 || …) { body }` — a guard CHAIN of K>=2
    two-way conditional blocks that converge on a single BODY block, with exactly one other
    exit (the SKIP / continuation). Distinct from boolean_regions (value-diamonds: K conds +
    2 value-LOAD arms → `cond ? a : b`); HERE the two arms are CONTROL destinations (the body
    runs statements; the skip bypasses), so the inverse is a compound `if`, not a ternary.

    This is the forward shape `if(a||b){…}` lowers to (lowering-atlas `b_or`): the body block
    gets a SECOND entry edge from the chain, which a plain region tree reads as a cross-edge
    and bails — leaving the goto-into-block residue (`$9A5D`). The fix is to recover the
    compound condition (reusing `recover_bool_formula`, with {body, skip} as its two terminals)
    and contract the chain to a single `entry → {body, skip}` branch in `bytecode_cfg`, exactly
    as boolean_regions/value_diamonds are contracted, so the gate stays consistent.

    Returns region dicts {entry, body, skip, cond, cinfo, value_blocks, cond_branch_addrs} for
    the decoder + bytecode_cfg. Same soundness guards as boolean_regions: a single chain entry,
    every chain branch stays in {chain, body, skip}, and FORWARD layout (entry = min, the two
    exits past the chain) so the fold is address-monotonic. Mutually exclusive with the value /
    boolean regions (a chain whose body is a value-arm is theirs)."""
    cfg, leaders = _bytecode_cfg_raw(instructions)
    block_of = _block_of_fn(leaders)
    bmap = {L: [] for L in leaders}
    for ins in instructions:
        bmap[block_of(ins['addr'])].append(ins)
    preds = _predecessors(cfg)

    def is_cond(b):
        blk = bmap.get(b, [])
        return bool(blk) and blk[-1]['mnemonic'] in ('branch_z_abs', 'branch_nz_abs') \
            and len(cfg.get(b, ())) == 2

    # blocks already owned by a value/boolean region — those are expression folds, not control.
    taken_value = set()
    for d in value_diamonds(instructions):
        taken_value |= {block_of(d['head']), d['taken_block'], d['fall_block']}
    for r in boolean_regions(instructions):
        taken_value |= r['cond'] | r['value_blocks']

    regions, used = [], set()
    for body in leaders:
        if body in used or body in taken_value:
            continue
        # PURE || : every guard short-circuits DIRECTLY to the body, so the chain is exactly
        # body's conditional predecessors (no BFS — bounded, and it won't swallow an upstream
        # cond the way a predecessor-walk would). (Mixed (a&&b)||c, where an inner guard does
        # NOT branch straight to body, is a separate, later atom.)
        chain = {p for p in preds.get(body, ()) if is_cond(p) and not (p in taken_value)}
        if len(chain) < 2:
            continue
        # Each guard's OTHER successor (besides body) must be another guard or the single skip.
        nonbody = {c: next(s for s in cfg[c] if s != body) for c in chain}
        skips = {nonbody[c] for c in chain if nonbody[c] not in chain}
        if len(skips) != 1:
            continue
        (skip,) = tuple(skips)
        if skip is EXIT:                              # keep the merge a real leader for now
            continue
        # Linear chain: exactly one entry (not the fall-target of another guard); the rest are
        # each some guard's fall-target. A guard branching to body twice (body == nonbody) can't
        # happen (cfg[c] has 2 distinct succs).
        targeted = {nonbody[c] for c in chain} & chain
        entries = [c for c in chain if c not in targeted]
        if len(entries) != 1 or len(targeted) != len(chain) - 1:
            continue
        entry = entries[0]
        # Non-entry guards must be reached ONLY from inside the chain — bytecode_cfg removes them,
        # so an external predecessor would dangle (boolean_regions' "value arm reached from outside"
        # guard, for the control case).
        if any(p not in chain for c in chain if c != entry for p in preds.get(c, ())):
            continue
        # FORWARD layout only (address-monotonic fold): entry first, the two exits past the chain.
        if entry != min(chain) or body <= max(chain) or skip <= max(chain):
            continue
        # SOUNDNESS (gate-invisible): a NON-entry guard is only evaluated when the earlier ones
        # short-circuited false; folding hoists its block before the compound branch, so it would
        # run UNCONDITIONALLY. Reject unless every non-entry guard block is side-effect-free (no
        # store / call / switch / syscall — incl. a call-valued condition). The entry guard runs
        # unconditionally either way, so its side effects are fine.
        def _pure(b):
            return all(('store' not in i['mnemonic'] and 'call' not in i['mnemonic']
                        and not i['mnemonic'].startswith('syscall') and i['mnemonic'] != 'switch')
                       for i in bmap.get(b, [])[:-1])
        if not all(_pure(c) for c in chain if c != entry):
            continue
        cinfo = {}
        for c in chain:
            br = bmap[c][-1]
            tgt = block_of(_target(br))
            fall = next(s for s in cfg[c] if s != tgt)
            # branch_z (JUMPF) jumps when cond FALSE; branch_nz (JUMPT) when TRUE.
            tsucc, fsucc = (fall, tgt) if br['mnemonic'] == 'branch_z_abs' else (tgt, fall)
            cinfo[c] = {'branch_addr': br['addr'], 'true': tsucc, 'false': fsucc}
        tail = next(c for c in chain if nonbody[c] == skip)   # guard whose other exit is skip
        # The tail must FALL THROUGH to the body (standard `if(!last) goto skip; body…` layout),
        # and the chain must be address-contiguous [entry, body) — so after bytecode_cfg removes
        # the middle guards, `body` becomes the entry block's fall-through (next leader) and the
        # decoder's compound `if(!(c1||…)) goto skip` re-lowers to exactly entry→{body, skip}.
        # Without this, lower_raw diverges from the contraction (the witness gate breaks).
        ti = leaders.index(tail)
        orig_next_tail = leaders[ti + 1] if ti + 1 < len(leaders) else EXIT
        if orig_next_tail != body:
            continue
        if {L for L in leaders if entry <= L < body} != chain:
            continue
        used |= chain
        regions.append({
            'entry': entry, 'body': body, 'skip': skip, 'cond': chain, 'cinfo': cinfo,
            'tail': tail, 'tail_branch_addr': cinfo[tail]['branch_addr'],
            'value_blocks': {body, skip},          # recover_bool_formula's two terminals
            'cond_branch_addrs': {cinfo[c]['branch_addr']: c for c in chain},
        })
    return regions


# Immediate pushes safe to treat as a SHARED prefix ahead of a consuming call (path-independent
# constants — value can't differ by which pred reached the merge). pushA/local/global pushes are
# excluded: those can carry a per-arm value, so they must be phi'd, not shared.
_SHARED_PREFIX_PUSHES = frozenset(('push_imm4', 'push_imm_word', 'push_imm_sbyte', 'op_8D_sbyte'))


def consuming_phis(instructions):
    """Find consuming-call STACK-PHI merges, and name the temp each arm's push materialises into.

    The decoder is a single LINEAR address-order sweep over ONE shared data stack with no phi
    reconciliation at control-flow joins. When a value pushed in one block is consumed by a CALL
    in a merge block reached from several predecessors that each push a DIFFERENT value (two arms
    each `push msg; …`, a shared `redraw_window(msg)` at the join), the call pops only the
    fall-through pred's push and the JUMPing preds' pushes LEAK and are silently DROPPED
    (confirmed: `msg_storehouse_full` etc. never appear in any output). A structural gate can't
    see it (two structurings share a CFG yet differ on the value at the merge).

    FIX (phi destruction via a temp): materialise each arm's push into a per-merge TEMP, so the
    merge's call pops the temp. All arms write the SAME name, so the linear leak is harmless, and
    each arm's assignment carries its own value under its own control — sound, no re-evaluation
    (unlike a reaching-condition ternary, which would re-run a call-valued branch predicate).

    Returns `{materialise_addr: (merge, arity, tag_addr)}`. At each predecessor's EXIT to the merge
    (its unconditional jump, or — for the fall-through pred — the merge's call address = the capture
    SITE), the decoder stores the top `arity` stack entries (the call's args, fully built — works for
    COMPUTED args, not just constant/load pushes) into per-merge temps `phi_<merge>_<k>` and leaves
    the temp names on the stack, so the merge's call pops the temps. The `tag_addr` (the PRED's own
    block) is where DREAM ATTRIBUTES the materialise line — the fall pred's else block, not the join. All preds write the SAME names, so the
    linear leak is harmless and each arg is carried under its own control — sound, no re-eval.

    CONSERVATIVE GUARDS (only a genuine multi-path arg-merge is touched): the merge's FIRST op is a
    consuming `host_call`; ≥2 predecessors whose blocks are NOT all identical (else same args, no
    phi); and every pred leaves to the merge by an unconditional jump or fall-through (a pred
    ending in a conditional branch is skipped). Mutually exclusive with value/boolean/short-circuit
    regions by construction (their merges aren't a leading consuming call)."""
    cfg, leaders = _bytecode_cfg_raw(instructions)
    block_of = _block_of_fn(leaders)
    bmap = {L: [] for L in leaders}
    for ins in instructions:
        bmap[block_of(ins['addr'])].append(ins)
    preds = _predecessors(cfg)

    def blk_sig(p):
        return tuple((i['mnemonic'], i.get('operand', '') or '') for i in bmap.get(p, []))

    out = {}
    for M in leaders:
        ps = [p for p in preds.get(M, ()) if p is not EXIT]
        if len(ps) < 2:
            continue
        blk = bmap.get(M, [])
        if not blk:
            continue
        # The consuming call may sit behind a SHARED-ARG PREFIX: a run of IMMEDIATE pushes in the
        # merge block that build the call's TOP args. These execute ONCE in M (after the join), so
        # they are identical on every path — only the args BELOW them are phi'd per arm ($D2A2's
        # $D2EE: `push 0; push 3; syscall_dispatch(…,$0C)` — selector+arg shared, 4 args phi'd).
        # Immediates only: a `pushA`/local/global prefix could carry a per-arm value, so skip those
        # (the call must then BE the first op, the original arity-N-at-block-start case).
        call_i = next((j for j, i in enumerate(blk)
                       if i['mnemonic'] in ('host_call', 'host_call_simple')), None)
        if call_i is None:
            continue
        if any(blk[j]['mnemonic'] not in _SHARED_PREFIX_PUSHES for j in range(call_i)):
            continue
        shared_k = call_i                              # each prefix op pushes exactly one word
        bp = re.search(r',\s*\$([0-9A-Fa-f]+)\s*$', blk[call_i].get('operand', '') or '')
        n = (int(bp.group(1), 16) // 2 if bp else 0) - shared_k   # per-arm arity = total - shared
        if n < 1:
            continue
        if len({blk_sig(p) for p in ps}) < 2:          # all preds identical -> same args, no phi
            continue
        # Each pred must leave to M either by an UNCONDITIONAL jump or by FALL-THROUGH (its args
        # are then fully built on the stack at that exit). Skip a merge whose pred ends in a
        # conditional branch (args may be mid-expression / the other edge complicates it).
        sites, ok = {}, True
        for p in ps:
            pb = bmap.get(p, [])
            if not pb:
                ok = False
                break
            last = pb[-1]
            # site = capture TIMING (args fully built on the stack there); tag = the PRED's own block
            # addr, so DREAM buckets the materialise INTO the arm, not after the join (the DREAM-vs-
            # linear placement lesson -- mirrors return_phis/value_merge_phis; a merge-addr tag floats
            # the fall pred's capture past the if and clobbers the jump arms in DREAM's structured C).
            if last['mnemonic'] == 'jump_abs' and _target(last) == M:
                sites[last['addr']] = last['addr']     # jump pred: site == tag (in the pred's block)
            elif last['mnemonic'] not in ('jump_abs', 'branch_z_abs', 'branch_nz_abs', 'switch'):
                sites[blk[0]['addr']] = last['addr']   # fall pred: site = merge call, tag = its block
            else:
                ok = False
                break
        if not ok:
            continue
        for addr, tag in sites.items():
            out[addr] = (M, n, tag)                    # (merge, arity, tag) -> materialise top-N
    return out


def return_phis(instructions):
    """Find RETURN-PHI merges: a bare `vm_return` block reached by >=2 preds that leave with a
    DIFFERENT regA (the return value), and name the temp each pred materialises it into.

    Same root cause as `consuming_phis`, on regA at the return instead of a call's stack args.
    The decoder is a single LINEAR sweep: `return {regA}` at a shared bare RETURN emits only the
    value of whichever pred is TEXTUALLY adjacent (the fall-through), and the JUMPing preds' regA
    is dropped ($D2A2's $D2F8: the arg1==0 arm `return syscall_dispatch(19)` collapses to the
    fall arm's `return 200`). One arm here is CALL-valued, so it must be captured at the pred's
    exit (a temp), never re-evaluated at the return.

    Returns `{site_addr: (merge, tag_addr)}`. The capture SITE fixes the timing (where regA is
    grabbed, pre-dispatch); the TAG fixes DREAM block attribution (the pred's OWN block, so the
    materialise lands INSIDE the arm, not after the join -- the DREAM-vs-linear placement lesson
    from value_merge_phis). Sites: a JUMPing pred's jump, a conditional-branch pred's branch (its
    TAKEN edge flows into the merge; branch_z/nz don't touch regA, so regA there == the taken-edge
    return value), or the RETURN address for the fall-through pred. The decoder stores regA into
    `phi_ret_<merge>`; the merge's `vm_return` returns that temp. All preds write the SAME name.

    GUARDS (mirror consuming_phis): the merge block is a BARE `vm_return` (so regA is carried in,
    not recomputed in-block); >=2 preds; each leaves by an unconditional jump, a conditional branch
    INTO the merge, or fall-through (a branch whose taken edge is NOT the merge, or a switch, bails);
    and the preds' blocks are NOT all identical (>=2 distinct signatures -> the returned regA
    genuinely differs; identical preds return the same value). The conditional-branch case (added
    2026-06-10) closed the $E80C class: 21 subs were firing a side-effecting call UNCONDITIONALLY
    (the call-valued fall arm leaked to the RETURN) + 28 more had value-only regA leaks."""
    cfg, leaders = _bytecode_cfg_raw(instructions)
    block_of = _block_of_fn(leaders)
    bmap = {L: [] for L in leaders}
    for ins in instructions:
        bmap[block_of(ins['addr'])].append(ins)
    preds = _predecessors(cfg)
    # A value-diamond / boolean-region merge can ALSO be a bare RETURN (min_word returns
    # `(a<b)?a:b`); those fold to a ternary, the BETTER rendering, so leave them to that pass.
    folded_merges = ({d['merge'] for d in value_diamonds(instructions)}
                     | {r['merge'] for r in boolean_regions(instructions)})

    def blk_sig(p):
        return tuple((i['mnemonic'], i.get('operand', '') or '') for i in bmap.get(p, []))

    out = {}
    for M in leaders:
        if M in folded_merges:                                     # handled by the ternary fold
            continue
        blk = bmap.get(M, [])
        if len(blk) != 1 or blk[0]['mnemonic'] != 'vm_return':     # bare RETURN block only
            continue
        ps = [p for p in preds.get(M, ()) if p is not EXIT]
        if len(ps) < 2:
            continue
        if len({blk_sig(p) for p in ps}) < 2:                      # all preds identical -> same regA
            continue
        sites, ok = {}, True
        for p in ps:
            pb = bmap.get(p, [])
            if not pb:
                ok = False
                break
            last = pb[-1]
            # Each entry: capture-SITE addr -> (merge, TAG addr). Site = where regA is captured
            # (pre-dispatch timing); tag = the PRED's own block addr, so DREAM buckets the
            # materialise INTO the arm, not after the join (the DREAM-vs-linear placement lesson
            # from value_merge_phis -- a merge-addr tag floats the capture past the if).
            if last['mnemonic'] == 'jump_abs' and _target(last) == M:
                sites[last['addr']] = (M, last['addr'])            # capture before the jump flushes
            elif last['mnemonic'] in ('branch_z_abs', 'branch_nz_abs') and _target(last) == M:
                # TAKEN edge of a conditional branch flows straight into the merge ($E80C's $E822:
                # `if(flags&4)==0 -> RETURN`). branch_z/nz emit `if(regA) goto` and DON'T touch regA,
                # so regA on the taken edge == regA before the branch -> capture there. Without this
                # the merge was abandoned and a call-valued fall-through arm leaked to the RETURN,
                # firing UNCONDITIONALLY (the $E80C class; 49 merges corpus-wide).
                sites[last['addr']] = (M, last['addr'])
            elif last['mnemonic'] not in ('jump_abs', 'branch_z_abs', 'branch_nz_abs', 'switch'):
                # fall-through: capture at the RETURN (regA = the fall arm's value/call result there),
                # but TAG to the fall pred's last instr so DREAM places it in the fall/else block.
                sites[blk[0]['addr']] = (M, pb[-1]['addr'])
            else:
                ok = False
                break
        if not ok:
            continue
        for addr, mt in sites.items():
            out[addr] = mt
    return out


def push_phis(instructions):
    """Find PUSH-PHI merges: a block whose FIRST op is `pushA` (push regA onto the data stack),
    reached by >=2 preds that leave with a DIFFERENT regA, and name the temp each pred materialises.

    Same root cause as `return_phis`, with the consumer a `pushA` instead of `vm_return`. The
    decoder is a single LINEAR sweep, so the shared push emits only the regA of the textually
    adjacent (fall-through) pred and the JUMPing preds' regA LEAKS and is DROPPED. The pushed
    value then usually feeds a multi-pred consuming CALL (`consuming_phis`), so the two chain:
    push_phis fixes the value at the shared push, consuming_phis carries it to the call.
    ($83FA effect_view_a's $84B5: a 3-way push of {table-lookup, msg_home_fief, msg_direct} into
    a shared `redraw_window` -- the AI-state-true table-lookup arm was dropped and every path
    rendered `redraw_window(msg_home_fief)`. value_diamonds can't fold it: it's a 3-way merge,
    not a 2-way head.)

    Returns `{site_addr: (merge, tag_addr)}`; callers read `set(v[0] for v in values())` for the
    merge set (the `pushA` there pushes `phi_push_<merge>`, not the linearly-stale regA). At each
    site -- a JUMPing pred's jump, or the `pushA` address itself for the fall-through pred -- the
    decoder stores regA into `phi_push_<merge>`, tagged at `tag_addr` (the pred's block) for DREAM. All preds write the SAME name, so the linear
    leak is harmless and each value is carried under its own control -- sound, no re-evaluation.

    GUARDS (mirror return_phis): the merge block's FIRST op is `pushA`; >=2 preds; each leaves by an
    unconditional jump or fall-through (a conditional-branch pred is skipped); preds' blocks are NOT
    all identical (>=2 distinct signatures -> regA genuinely differs); and NOT a value-diamond /
    boolean merge (those fold to a ternary -- the better rendering, and a 2-way regA diamond ending
    in a push is already handled there)."""
    cfg, leaders = _bytecode_cfg_raw(instructions)
    block_of = _block_of_fn(leaders)
    bmap = {L: [] for L in leaders}
    for ins in instructions:
        bmap[block_of(ins['addr'])].append(ins)
    preds = _predecessors(cfg)
    folded_merges = ({d['merge'] for d in value_diamonds(instructions)}
                     | {r['merge'] for r in boolean_regions(instructions)})

    def blk_sig(p):
        return tuple((i['mnemonic'], i.get('operand', '') or '') for i in bmap.get(p, []))

    out = {}
    for M in leaders:
        if M in folded_merges:                                     # handled by the ternary fold
            continue
        blk = bmap.get(M, [])
        if not blk or blk[0]['mnemonic'] != 'pushA':               # consumer must be a regA push
            continue
        ps = [p for p in preds.get(M, ()) if p is not EXIT]
        if len(ps) < 2:
            continue
        if len({blk_sig(p) for p in ps}) < 2:                      # all preds identical -> same regA
            continue
        sites, ok = {}, True
        for p in ps:
            pb = bmap.get(p, [])
            if not pb:
                ok = False
                break
            last = pb[-1]
            # site = capture timing; tag = the PRED's own block addr (DREAM attribution), so the fall
            # pred's materialise lands in its else block, not after the join (mirrors return_phis).
            if last['mnemonic'] == 'jump_abs' and _target(last) == M:
                sites[last['addr']] = last['addr']                  # jump pred: site == tag (pred block)
            elif last['mnemonic'] not in ('jump_abs', 'branch_z_abs', 'branch_nz_abs', 'switch'):
                sites[blk[0]['addr']] = last['addr']                # fall pred: site = pushA, tag = block
            else:
                ok = False
                break
        if not ok:
            continue
        for addr, tag in sites.items():
            out[addr] = (M, tag)                                    # (merge, tag_addr)
    return out


_STORE_REGA = {'storeA_local_neg', 'storeA_local_pos', 'storeA_mem_word', 'storeA_mem_byte'}
_BRANCH_REGA = {'branch_z_abs', 'branch_nz_abs'}
# A DEREF (regA = *(regA), opcode $B0) also CONSUMES regA-in (the address) — so a merge whose
# first op is a deref reached by preds leaving DIFFERENT addresses (e.g. a switch where each case
# computes side_resource_ptr(arg1)+{0,2,4}, $8B8A) drops all but the fall pred's address without
# this. The decoder side is consumer-agnostic (it sets state.regA to the temp; the deref then
# reads `*(phi_val)`), so allowing it here is the whole fix. (bug 1.2, 2026-06-11.)
_DEREF_REGA = {'loadA_ind_word'}


def value_merge_phis(instructions):
    """Find regA VALUE-MERGE phis with SIDE-EFFECTING arms: a merge whose FIRST op CONSUMES regA --
    a store of regA to a local/global (`storeA_*`) or a branch on regA (`branch_z/nz_abs`) -- reached
    by >=2 preds that leave a DIFFERENT regA. Name the temp each pred materialises it into.

    The regA analogue of return_phis/push_phis for the cases value_diamonds CAN'T fold: the arms do
    side-effecting work (a per-arm store, an rng CALL) AND leave a different regA, so the
    side-effect-free `_value_arm` test rejects them and the single LINEAR sweep then keeps only the
    fall-through pred's regA -- silently DROPPING the JUMPing preds' value (a gate-INVISIBLE value
    bug: two structurings share the CFG yet differ on the value at the merge). Found by
    hand-decompiling `$A45A` (the AI strategic-turn planner) from bytecode and diffing vs V2:
      - `$A4FD` (a `storeA_local`): the event-handler pointer
        `(rng_mod(4)==0) ? random_event_ravage_output_hidden_mark_weakness : ai_event_build_…`
        collapsed to the fall arm `ai_event_build_…` -> the WRONG random event ran when rng_mod(4)==0.
      - `$A523` (a `branch_z`): the per-province event chance
        `(select!=3) ? (rng_mod(40)<3) : roll_3pct_event_chance()` collapsed to `roll_3pct()` -> the
        wrong selection probability for event modes 1/2.

    Returns `{site_addr: (merge, tag_addr)}`. At each site -- a JUMPing pred's jump, or the merge's
    first op for the fall-through pred -- the decoder stores regA into `phi_val_<merge>`; the
    store/branch then reads that temp (it already reads state.regA, which the materialisation sets to
    the temp). All preds write the SAME name, so the linear leak is harmless and each value is
    captured under its own control -- sound, no re-evaluation (a CALL-valued arm's call is read
    exactly once, so rng advances exactly as the bytecode does).

    `tag_addr` is the address the materialise LINE is attributed to (NOT necessarily the firing
    site). It MUST land in the pred's OWN block: the jump pred's jump addr (its then block), and --
    the trap -- the FALL-THROUGH pred's last-instr addr (its else block), NOT the merge. DREAM
    assigns statements to blocks by addr (vm_reduce._partition); a merge-addr tag buckets the fall
    capture into the MERGE block, so DREAM emits it AFTER the if/else join -- where it runs
    unconditionally, clobbering every jump arm's value and leaving an empty else ($A45A $A523/$A4FD).
    The earlier fix tagged the fall capture at the consumer (merge) and relied on the LINEAR
    emitter's goto-bypass for placement; that is correct linearly but DREAM-WRONG, because DREAM
    dissolves the bypass. The repair must be DREAM-correct (right block), not just linear-correct.
    The linear emitter is unaffected by the retag (emission order, not addr, fixes ITS placement).

    GUARDS (mirror return_phis/push_phis): the merge's FIRST op consumes regA (store/branch -> regA
    is carried IN, not recomputed in-block); >=2 preds; each leaves by an unconditional jump or
    fall-through (a conditional-branch pred is skipped -- its regA is its own branch condition, not a
    merged value); preds' blocks are NOT all identical (>=2 distinct signatures -> regA genuinely
    differs); and NOT a value-diamond / boolean merge (those fold to a ternary -- the better form)."""
    cfg, leaders = _bytecode_cfg_raw(instructions)
    block_of = _block_of_fn(leaders)
    bmap = {L: [] for L in leaders}
    for ins in instructions:
        bmap[block_of(ins['addr'])].append(ins)
    preds = _predecessors(cfg)
    folded_merges = ({d['merge'] for d in value_diamonds(instructions)}
                     | {r['merge'] for r in boolean_regions(instructions)})

    def blk_sig(p):
        return tuple((i['mnemonic'], i.get('operand', '') or '') for i in bmap.get(p, []))

    out = {}
    for M in leaders:
        if M in folded_merges:                                     # handled by the ternary fold
            continue
        blk = bmap.get(M, [])
        if not blk or blk[0]['mnemonic'] not in (_STORE_REGA | _BRANCH_REGA | _DEREF_REGA):
            continue                                               # consumer must read regA first
        ps = [p for p in preds.get(M, ()) if p is not EXIT]
        if len(ps) < 2:
            continue
        if len({blk_sig(p) for p in ps}) < 2:                      # all preds identical -> same regA
            continue
        sites, ok = {}, True
        for p in ps:
            pb = bmap.get(p, [])
            if not pb:
                ok = False
                break
            last = pb[-1]
            if last['mnemonic'] == 'jump_abs' and _target(last) == M:
                # JUMP pred: fire at the jump (before it flushes regA); tag = jump addr (its block).
                sites[last['addr']] = (M, last['addr'])
            elif last['mnemonic'] not in ('jump_abs', 'branch_z_abs', 'branch_nz_abs', 'switch'):
                # FALL-THROUGH pred: regA isn't live until its last op dispatches, so fire at the
                # consumer (merge's first op) but tag with the fall pred's last-instr addr so the
                # line buckets into the FALL (else) block, not the merge -- see the docstring.
                sites[blk[0]['addr']] = (M, last['addr'])
            else:
                ok = False                                         # a conditional-branch pred -> skip merge
                break
        if not ok:
            continue
        out.update(sites)
    return out


def unknown_control_mnemonics(instructions):
    """Expose any unrecognised control-shaped mnemonics for the gate to report."""
    if not instructions:
        return []
    succ, *_rest, unknown = _instr_succ(instructions)
    return unknown


# --- dominators / natural loops (the Phase-1 loop-structuring substrate) --------
# Standard iterative dataflow over the block CFG. Keyed on leader addrs + EXIT, the
# same coordinate system everything else here uses. These let the decompiler find
# the back edges that become `while`/`do-while` and the loop bodies they enclose.

def dominators(cfg, entry):
    """dom[n] = set of blocks that dominate n (every path entry->n passes through
    them). Iterative fixpoint; cfg = {leader: frozenset(succ)} incl. the EXIT node."""
    nodes = set(cfg) | {EXIT}
    preds = {n: set() for n in nodes}
    for n, succ in cfg.items():
        for s in succ:
            preds.setdefault(s, set()).add(n)
    dom = {n: set(nodes) for n in nodes}
    dom[entry] = {entry}
    changed = True
    while changed:
        changed = False
        for n in nodes:
            if n == entry:
                continue
            new = set(nodes)
            for p in preds[n]:
                new &= dom[p]
            new.add(n)
            if new != dom[n]:
                dom[n] = new
                changed = True
    return dom


def back_edges(cfg, entry):
    """The list of (u, h) edges where h dominates u — i.e. u->h closes a loop whose
    header is h. (Requires a reducible region for h to be the natural header; the
    natural_loop builder below is only meaningful on these.) Returns (edges, dom)."""
    dom = dominators(cfg, entry)
    edges = []
    for u, succ in cfg.items():
        for h in succ:
            if h is not EXIT and h in dom.get(u, ()):
                edges.append((u, h))
    return edges, dom


def natural_loop(u, h, cfg):
    """The natural loop of back edge u->h: {h} plus every node that can reach u
    without going through h. Standard backward worklist seeded from u and stopping at
    h — h is added up front and never pushed, so its own predecessors stay OUT (the
    header is the loop's single entry). A self-loop (u == h) is just {h}."""
    preds = {n: set() for n in cfg}
    for n, succ in cfg.items():
        for s in succ:
            if s is not EXIT:
                preds.setdefault(s, set()).add(n)
    loop = {h}
    if u != h:
        loop.add(u)
        work = [u]
        while work:
            n = work.pop()
            for p in preds.get(n, ()):
                if p not in loop:
                    loop.add(p)
                    work.append(p)
    return loop


# --- switch dispatch regions (Phase 3: inline the case BODIES) ------------------
# The shared detector both the decompiler's structure_switches emit pass AND the gate's
# lower_struct_cfg consume — same rule, so they can't disagree (the value_diamonds /
# boolean_regions pattern). A switch is FOLDABLE (its case bodies can move inside the
# braces) iff its dispatch region is SINGLE-ENTRY: every case-target block is dominated by
# the switch block S, so the only way into the case bodies is through S. The braces enclose
# the contiguous address range (S, max region addr]; nothing is reordered (lexical order ==
# address order is preserved, so the address-based lowering stays faithful AND the emitted C
# is correct). A case body whose every edge can't fall through keeps an honest internal goto.

def post_dominators(cfg):
    """pdom[n] = blocks every path n->EXIT passes through (dual of `dominators` over the
    reversed CFG with EXIT as the single sink)."""
    nodes = set(cfg) | {EXIT}
    pdom = {n: set(nodes) for n in nodes}
    pdom[EXIT] = {EXIT}
    changed = True
    while changed:
        changed = False
        for n in cfg:
            new = set(nodes)
            for s in cfg.get(n, ()):
                new &= pdom.get(s, {EXIT})
            new.add(n)
            if new != pdom[n]:
                pdom[n] = new
                changed = True
    return pdom


def _imm_post_dom(pdom, n):
    """The immediate (nearest) post-dominator of n: the strict post-dom b such that every
    other strict post-dom c post-dominates b (c is farther from n). May be EXIT."""
    cand = [b for b in pdom.get(n, ()) if b != n]
    for b in cand:
        if all((c in pdom.get(b, set())) for c in cand if c != b):
            return b
    return EXIT


def switch_dispatches(instructions):
    """Foldable switch dispatch regions, one dict per `switch` whose case bodies form a
    SINGLE-ENTRY region (every case target dominated by the switch block). Dict fields:
      switch_addr   : addr of the switch instruction (= the dispatch lines' addr)
      S             : leader of the switch block
      key_targets   : [(key_int, target_block), ...] in declaration order (keys may repeat
                      a target; default is appended as key=None)
      has_default   : bool
      enclosed      : sorted leaders the braces enclose = leaders in (switch_addr, brace_close]
      brace_close   : the highest enclosed leader addr (the `}` goes after this block)
      merge         : immediate post-dominator of S (the switch's natural exit; may be EXIT)
      break_target  : merge, IFF it is a real block laid out cleanly AFTER the region
                      (addr > brace_close) — then edges to it may render as `break;`. Else
                      None (the merge is interleaved / returns -> convergence stays goto).
    A switch whose region is NOT single-entry, or whose enclosed span would swallow a block
    NOT dominated by S (external code), is omitted -> it stays goto-cases (honest)."""
    if not instructions:
        return []
    cfg, leaders = bytecode_cfg(instructions)
    if not leaders:
        return []
    block_of = _block_of_fn(leaders)
    dom = dominators(cfg, leaders[0])
    pdom = post_dominators(cfg)
    lead_set = set(leaders)
    out = []
    for ins in instructions:
        if ins['mnemonic'] != 'switch':
            continue
        sa = ins['addr']
        S = block_of(sa)
        comment = ins['comment'] or ''
        key_targets = [(int(k), block_of(int(t, 16)))
                       for k, t in re.findall(r'(-?\d+)=>\$([0-9A-Fa-f]{4})', comment)]
        dflt = re.search(r'default=>\$([0-9A-Fa-f]{4})', comment)
        has_default = dflt is not None
        if has_default:
            key_targets.append((None, block_of(int(dflt.group(1), 16))))
        if not key_targets:
            continue
        targets = {t for _k, t in key_targets}
        # SINGLE-ENTRY: every case target dominated by S (no external jump into a body).
        if not all(S in dom.get(t, set()) for t in targets):
            continue
        M = _imm_post_dom(pdom, S)
        # region = S-dominated, NOT M-dominated (strictly between the switch and its merge).
        region = {b for b in leaders
                  if S in dom.get(b, set()) and b != S
                  and not (M is not EXIT and M in dom.get(b, set()))}
        if not region:
            continue
        brace_close = max(region)
        # The braces enclose (switch_addr, brace_close]; every leader in that span must be
        # S-dominated (no external code swallowed). The merge, if interleaved, sits inside.
        enclosed = sorted(L for L in leaders if sa < L <= brace_close)
        if not all(S in dom.get(L, set()) for L in enclosed):
            continue
        # default_is_merge: the `default` target IS the switch's post-dom merge M — i.e. an
        # EMPTY default (the `$A30D`/atom-4 shape: every case body → M, default → M directly).
        # Then M is the shared switch EXIT, not a case body: the cases render `break;` and M is
        # pulled out past the `}`. break_target is set to M even when M is INTERLEAVED by address
        # (M < brace_close) — the label-aware gate (merge_after / switch break-context) tolerates
        # the out-of-address-order placement, which is the whole point of atom 4.
        default_tgt = next((t for k, t in key_targets if k is None), None)
        default_is_merge = has_default and default_tgt == M and M is not EXIT
        break_target = (M if (M is not EXIT and M in lead_set
                              and (M > brace_close or default_is_merge)) else None)
        # body_end = the leader where the territory ENDS (the first leader past the last
        # enclosed BLOCK). The braces enclose every line up to but not including it; None =>
        # the territory runs to the end of the function. (brace_close is the last enclosed
        # block's LEADER; its body lines extend to body_end, so the emit must not stop at the
        # leader — that would cut the last case body off mid-block.)
        later = [L for L in leaders if L > brace_close]
        body_end = min(later) if later else None
        out.append({
            'switch_addr': sa, 'S': S, 'key_targets': key_targets,
            'has_default': has_default, 'enclosed': enclosed, 'brace_close': brace_close,
            'body_end': body_end, 'merge': M, 'break_target': break_target,
            'default_is_merge': default_is_merge,
        })
    return out


# --- lowering a C line list (raw goto OR structured) to the block CFG ----------

def _classify_stmt(text):
    """(kind, target_addr) for a leaf C statement. kinds:
    goto / ifgoto (cond -> target, else fall) / return / ifreturn (cond -> EXIT,
    else fall) / break / continue / ifbreak / ifcontinue (target is the enclosing
    loop's exit / header, resolved by the caller via loop context, so target_addr is
    None here) / plain (sequential fall-through)."""
    t = text.strip()
    m = re.match(r'goto L_([0-9A-Fa-f]{4});$', t)
    if m:
        return 'goto', int(m.group(1), 16)
    m = re.match(r'if \(.+\) goto L_([0-9A-Fa-f]{4});$', t)   # if(C) and if(!(C))
    if m:
        return 'ifgoto', int(m.group(1), 16)
    if t == 'break;':
        return 'break', None
    if t == 'continue;':
        return 'continue', None
    if re.match(r'if \(.+\) break;$', t):
        return 'ifbreak', None
    if re.match(r'if \(.+\) continue;$', t):
        return 'ifcontinue', None
    if re.match(r'return\b', t):
        return 'return', None
    if re.match(r'if \(.+\) return\b', t):       # structure_lines early-return idiom
        return 'ifreturn', None
    return 'plain', None


_RE_IF_OPEN     = re.compile(r'if \((.+)\) \{$')
_RE_WHILE_OPEN  = re.compile(r'while \((.+)\) \{$')
_RE_SWITCH_OPEN = re.compile(r'switch \((.+)\) \{$')
_RE_DOWHILE_CLOSE = re.compile(r'\} while \((.+)\);$')
_RE_LABEL       = re.compile(r'L_[0-9A-Fa-f]{4}:$')
_RE_SWITCH_CASE = re.compile(r'(case -?\d+|default): goto L_([0-9A-Fa-f]{4});$')
_RE_CASE_LABEL  = re.compile(r'(?:case -?\d+|default):$')   # Phase-3 INLINED case label


# --- true/false edge labels (catch a guard inversion / then-else swap) ----------
# The unlabelled CFG above treats a conditional block's two successors as an
# unordered set, so a fold that SWAPS the then/else bodies (or inverts the guard
# without compensating) preserves the edge SET and slips through. The fix: record,
# per 2-way conditional block, an ORIENTED (taken, fall) pair — taken = where control
# goes when the condition is TRUE — with all negation folded out so semantically equal
# guards compare equal. A branch condition is atomic here (the VM tests one regA value
# per branch), so it is either a bare value or a single `LHS op RHS` comparison; the
# only condition rewrites structuring performs are outer `!(...)` toggling and the
# comparison-operator flips in invert_condition (`<`<->`>=`, `>`<->`<=`, `==`<->`!=`).
# _canon_cond peels both into a parity bit, so raw and structured agree iff the routing
# is the same boolean function — and disagree exactly on a real swap.

_CMP_NEG = {'>=': '<', '<=': '>', '!=': '==', '<': '>=', '>': '<=', '==': '!='}
_RE_CMP = re.compile(r'^(.*?)\s*(<=|>=|==|!=|<|>)\s*(.*)$')


def _balanced_to_end(s, open_idx):
    """True iff the '(' at s[open_idx] has its matching ')' at the final char of s."""
    depth = 0
    for i in range(open_idx, len(s)):
        if s[i] == '(':
            depth += 1
        elif s[i] == ')':
            depth -= 1
            if depth == 0:
                return i == len(s) - 1
    return False


def _canon_cond(cond):
    """Canonicalise a branch condition to (base_text, parity). parity counts the
    negations folded out — outer `!(...)` wrappers AND a negated comparison operator —
    so two conditions that are boolean negations of each other share a base and differ
    in parity by exactly 1. Only parity is used by the orientation check; base is
    returned for diagnostics."""
    c = (cond or '').strip()
    parity = 0
    changed = True
    while changed:                                  # peel outer !(...) and redundant ( )
        changed = False
        if c.startswith('!(') and _balanced_to_end(c, 1):
            c, parity, changed = c[2:-1].strip(), parity ^ 1, True
        elif c.startswith('(') and _balanced_to_end(c, 0):
            c, changed = c[1:-1].strip(), True
    m = _RE_CMP.match(c)                             # single top-level comparison?
    if m:
        lhs, op, rhs = m.group(1).strip(), m.group(2), m.group(3).strip()
        if op in ('>=', '<=', '!='):                # fold negated op to its canonical mate
            op, parity = _CMP_NEG[op], parity ^ 1
        c = f"{lhs} {op} {rhs}"
    return c, parity


def _record_orient(orient, L, cond, taken, fall):
    """Store the parity-folded oriented edge for conditional block L: after folding the
    guard's negations out, `taken` is the TRUE side and `fall` the FALSE side."""
    if orient is None:
        return
    _base, parity = _canon_cond(cond)
    if parity:
        taken, fall = fall, taken
    orient[L] = (taken, fall)


def lower_goto_cfg(lines, leaders, orient=None):
    """Lower a RAW goto/label line list (no structured braces) to the block CFG.

    Per-block + ADDRESS-based, which makes it robust to two things a lexical /
    line-order walk gets wrong:
      * the decompiler's out-of-order flushed-call lines — a side-effecting CALL
        whose result is discarded is emitted at its FLUSH point (a later line) but
        tagged with its own EARLIER address (State.flush_pending); and
      * folded statement-less blocks — a branch target whose every instruction
        folds into an expression emits no line at all.
    In both cases fall-through must target the NEXT LEADER BY ADDRESS, never the
    block of the next emitted line. A block's successors are fixed by its single
    control terminator (goto / if-goto / return / switch), else it falls through.
    This is the witness-faithfulness gate (lower(raw) == bytecode_cfg).
    """
    if not leaders:
        return {}
    block_of = _block_of_fn(leaders)
    next_leader = {leaders[i]: (leaders[i + 1] if i + 1 < len(leaders) else EXIT)
                   for i in range(len(leaders))}
    blocks = {L: [] for L in leaders}
    for addr, _ind, text in lines:
        t = text.strip()
        if not t or t.startswith('//') or _RE_LABEL.match(t):
            continue
        if t == '}' or t == '} else {' or t.endswith('{') or _RE_DOWHILE_CLOSE.match(t):
            continue   # structured scaffolding has no place in the raw form; ignore defensively
        blocks[block_of(addr)].append(t)

    cfg = {}
    for L in leaders:
        succ = None
        sw_targets, sw_has_default, is_switch = set(), False, False
        for t in blocks[L]:
            mc = _RE_SWITCH_CASE.match(t)
            if mc:
                is_switch = True
                sw_targets.add(int(mc.group(2), 16))
                if mc.group(1) == 'default':
                    sw_has_default = True
                continue
            cl, tgt = _classify_stmt(t)
            if cl == 'goto':
                succ = {block_of(tgt)}
            elif cl == 'ifgoto':
                succ = {block_of(tgt), next_leader[L]}
                mcnd = re.match(r'if \((.+)\) goto', t)
                _record_orient(orient, L, mcnd.group(1), block_of(tgt), next_leader[L])
            elif cl == 'return':
                succ = {EXIT}
            elif cl == 'ifreturn':
                succ = {EXIT, next_leader[L]}
                mcnd = re.match(r'if \((.+)\) return', t)
                _record_orient(orient, L, mcnd.group(1), EXIT, next_leader[L])
            # plain statements contribute no edge; the block still falls through
        if is_switch:
            succ = {block_of(x) for x in sw_targets}
            if not sw_has_default:
                succ.add(next_leader[L])
        if succ is None:
            succ = {next_leader[L]}     # no terminator transfer => fall through
        cfg[L] = frozenset(succ)
    return cfg


def _is_scaffold(t):
    """A stripped line that carries no control AND no observable effect: blank,
    comment, label, or a brace/keyword opener/closer."""
    return (not t or t.startswith('//') or _RE_LABEL.match(t) is not None
            or _RE_CASE_LABEL.match(t) is not None        # Phase-3 inlined `case N:` marker
            or t == '}' or t == '} else {' or t.endswith('{')
            or _RE_DOWHILE_CLOSE.match(t) is not None)


def _opens_block(t):
    """A construct-OPENER (`if (C) {`, `while (C) {`, `switch (E) {`, `do {`). These
    end in `{` (so _is_scaffold calls them scaffold for the 'no effect' question) but
    they DO start a basic block — the header's condition-evaluation lives here. The
    while-loop lowering must count them when locating a body's first/last block."""
    return (_RE_IF_OPEN.match(t) is not None or _RE_WHILE_OPEN.match(t) is not None
            or _RE_SWITCH_OPEN.match(t) is not None or t == 'do {')


def _occupies_block(t):
    """A line that starts/holds a basic block: any non-scaffold statement, OR a
    construct opener (which is scaffold for the 'no effect' test but still begins a
    block — the header's condition lives there)."""
    return (not _is_scaffold(t)) or _opens_block(t)


def _loop_body_leaders(lines, open_idx, close_idx, block_of, leaders=None):
    """The basic-block leaders touched by a loop's body (the lines strictly between its
    opener `open_idx` and closer `close_idx`), counting label-only lines (whose addr IS
    a leader) and folded openers. Used to map each body block to its enclosing loop so a
    `break`/`continue` inside it resolves to that loop's exit/header.

    Line-LESS interior blocks are filled in: a basic block inside the body's address
    span that emits NO C line (e.g. a folded then-body tail with all instructions
    absorbed into an expression) is still part of the loop. Omitting it makes the
    rotated-while body-redirect misread it as an OUTSIDE block falling into the loop and
    wrongly route it through the header. `leaders` must be passed to enable the fill."""
    bls = set()
    for k in range(open_idx + 1, close_idx):
        t = lines[k][2].strip()
        m = _RE_LABEL.match(t)
        if m:
            bls.add(int(t[2:6], 16))
        elif _occupies_block(t):
            bls.add(block_of(lines[k][0]))
    if leaders and bls:
        lo, hi = min(bls), max(bls)
        bls.update(L for L in leaders if lo < L < hi)
    return bls


def _match_constructs(lines):
    """Brace-match the structured if/while/switch/do blocks. Returns (ifs, whiles,
    dowhiles, switches) where ifs = [(header_addr, header_idx, else_idx|None, closer_idx)],
    whiles = [(header_addr, header_idx, closer_idx)], dowhiles = [(do_addr, do_idx,
    close_addr, close_idx)], and switches = [(header_addr, header_idx, closer_idx)] for
    INLINED switches (Phase 3) whose successors come from the `case N:` labels in the span."""
    ifs, whiles, dowhiles, switches, stack = [], [], [], [], []
    for idx, (addr, _ind, text) in enumerate(lines):
        t = text.strip()
        if _RE_IF_OPEN.match(t):
            stack.append(['if', addr, idx, None])
        elif _RE_WHILE_OPEN.match(t):
            stack.append(['while', addr, idx])
        elif _RE_SWITCH_OPEN.match(t):
            stack.append(['switch', addr, idx])
        elif t == 'do {':
            stack.append(['do', addr, idx])
        elif t == '} else {':
            if stack and stack[-1][0] == 'if':
                stack[-1][3] = idx
        elif _RE_DOWHILE_CLOSE.match(t):
            if stack and stack[-1][0] == 'do':
                top = stack.pop()
                # do_addr (the `do {` line = loop header) + close_addr (the
                # `} while(C);` line = the tail TEST instruction's addr).
                dowhiles.append((top[1], top[2], addr, idx))
        elif t == '}':
            if not stack:
                continue
            top = stack.pop()
            if top[0] == 'if':
                ifs.append((top[1], top[2], top[3], idx))
            elif top[0] == 'while':
                whiles.append((top[1], top[2], idx))
            elif top[0] == 'switch':
                switches.append((top[1], top[2], idx))
    return ifs, whiles, dowhiles, switches


def _span_max_leader(lines, lo, hi, block_of):
    """The highest basic-block LEADER touched by lines[lo:hi], counting both real
    statements (via block_of) and label lines `L_XXXX:` (whose addr IS a leader).
    Labels matter: an empty / folded arm contributes no real statement but its label
    still marks the leader, so the merge / else-start lands past it. None if empty."""
    best = None
    for k in range(lo, hi):
        addr, _i, text = lines[k]
        t = text.strip()
        m = _RE_LABEL.match(t)
        if m:
            L = int(t[2:6], 16)
        elif _is_scaffold(t) and not _opens_block(t):
            continue                       # openers (`do {`, `if (C) {`, …) start a block
        else:
            L = block_of(addr)
        best = L if best is None else max(best, L)
    return best


def lower_struct_cfg(lines, leaders, orient=None):
    """Lower the STRUCTURED C (if/while/switch + braces) to the block CFG over
    `leaders`. Per-block + ADDRESS-based like lower_goto_cfg (same robustness to
    flushed-call reordering / folded blocks), except a block whose branch became an
    if/while HEADER takes its edges from the brace structure:
      if(C){then}            : header -> { next_leader(header) [then], merge }
      if(C){then}else{else}  : header -> { next_leader(header) [then], else_start }
      while(C){body}         : header -> { next_leader(header) [body], exit };  body-tail -> header
    Merge / else-start / exit are computed from the next leader AFTER the relevant
    span by address, so an empty or folded arm still resolves (and contraction then
    absorbs the routing blocks). Non-header blocks fall back to the per-block scan."""
    if not leaders:
        return {}
    # UN-SHARING: self-augment the leader set with any SYNTHETIC copy addresses in the structured
    # lines. They sort above every real addr, so real blocks' block_of / next_leader are unchanged
    # (inert when no copies exist); each duplicated copy becomes its own block.
    _synth = {a for a, _i, _t in lines if a >= _SYNTH_BASE}
    if _synth:
        leaders = sorted(set(leaders) | _synth)
    block_of = _block_of_fn(leaders)
    next_leader = {leaders[i]: (leaders[i + 1] if i + 1 < len(leaders) else EXIT)
                   for i in range(len(leaders))}

    # EMIT-ORDER fall-through (approach B — the general case of atom B's merge_after). The
    # address-based `next_leader[L]` is correct ONLY when emit order == address order. When the
    # reducer emits a block OUT of address order (a correct structuring of address-inverted code —
    # a cross-jump dup's reordered arm, a tail-merge), the address-next leader is the WRONG fall
    # target. The reducer's invariant: a reordered block is LABELLED (flush-safe — a label line's
    # addr IS the leader). So a forward walk records, for block L, the LEXICALLY-next block when it
    # is introduced by a LABEL that differs from next_leader[L] — that (and only that) overrides the
    # address fall. In-address-order emit (all of V1, all current V2) labels every block at its
    # address-next position, so `lex_fall` stays EMPTY and the lowering is unchanged — INERT until a
    # reordered emit needs it. Dedents (`}`) reset the run: an arm tail falls to its MERGE (handled
    # by merge_after / tail_redirect), not to a sibling block after the brace.
    lex_fall = {}
    _prev = None
    for _addr, _ind, _text in lines:
        _t = _text.strip()
        if _RE_LABEL.match(_t):
            _cur = int(_t[2:6], 16)
            # Record ONLY a BACKWARD reordered target (_cur < _prev). A genuine address-inverted
            # fall (the reducer emitting a lower-address block later — $AD38's AD7B->AD67) is always
            # backward; FLUSH only ever DELAYS a block's content FORWARD past a label (so the label
            # appears early, e.g. $A778's L_A7AD before A7A8's flushed body), which would mis-record
            # a forward target. The `< _prev` guard keeps the walk inert on flush-interleaved
            # address-ordered emit (raw / V1) while catching the real inversion (V2).
            if (_prev is not None and _prev is not EXIT and _prev not in lex_fall
                    and _cur < _prev):
                lex_fall[_prev] = _cur            # reordered (backward), label-anchored fall target
            _prev = _cur
            continue
        if _t == '}' or _t == '} else {' or _RE_DOWHILE_CLOSE.match(_t):
            _prev = None                          # scope close: arm tail -> merge, not a sibling
            continue
        if _is_scaffold(_t) and not _opens_block(_t):
            continue                              # blanks / comments / case labels
        _cur = block_of(_addr)                    # opener (reliable leader) or plain statement
        if _cur != _prev:
            _prev = _cur

    ifs, whiles, dowhiles, switches = _match_constructs(lines)

    # Phase-3 INLINED switches: the `switch (E) {` header block's successors are the
    # case-target blocks (the inline `case N:` labels in its brace span), plus the
    # selector-unmatched fall-through if there is no `default`. Same successor set the raw
    # `case N: goto L_X` form gives lower_goto_cfg, so the inlined render stays CFG-faithful.
    switch_edges = {}
    for sh_addr, sh_idx, sh_close in switches:
        sb = block_of(sh_addr)
        tgts, has_default = set(), False
        for k in range(sh_idx + 1, sh_close):
            ct = lines[k][2].strip()
            if _RE_CASE_LABEL.match(ct):
                tgts.add(block_of(lines[k][0]))
                if ct.startswith('default'):
                    has_default = True
        if tgts:
            succ = set(tgts)
            if not has_default:
                # no-default fall = the MERGE (first block after the switch `}`), NOT
                # next_leader[sb]: an inlined switch lays its case bodies right after the
                # header, so the header's unmatched-selector fall-through is the post-switch
                # block, not the first case. Label-aware (a pulled-out merge is labelled);
                # dedent / unlabelled-plain falls back to next_leader (the former behaviour).
                m = None
                for k in range(sh_close + 1, len(lines)):
                    tt = lines[k][2].strip()
                    if not tt or tt.startswith('//') or _RE_CASE_LABEL.match(tt):
                        continue
                    if _RE_LABEL.match(tt):
                        m = int(tt[2:6], 16)
                    elif tt == '}' or tt == '} else {' or _RE_DOWHILE_CLOSE.match(tt):
                        m = None
                    elif _opens_block(tt) or not _is_scaffold(tt):
                        m = block_of(lines[k][0])
                    break
                succ.add(m if m is not None else next_leader[sb])
            switch_edges[sb] = succ

    header_edges, back_edges, tail_redirect, dowhile_edges = {}, {}, {}, {}
    # A loop body-TAIL block falls off the bottom `}` back to the loop HEADER, not to the
    # block after the loop. For a rotated `while(C)` next_leader[tail] already IS the
    # header (the test sits right after the body by address), so this was invisible; for
    # `while(1)` (tail at the end, header at the top) it matters — modelling the fall as
    # next_leader fabricated a spurious exit edge that let a bad fold pass. fall_of()
    # routes a tail's fall to the header.
    tail_fall_to_header = {}
    while_bodies = []   # (header_block, frozenset(body_leaders)) per while construct

    def span_after(lo, hi, header_block):
        """Block reached just past the line span [lo,hi): the leader following the
        HIGHEST leader the span touches. Falls to the header's own next leader when
        the span is empty."""
        top = _span_max_leader(lines, lo, hi, block_of)
        return next_leader[top] if top is not None else next_leader[header_block]

    def first_real_block(lo, hi):
        """Block of the first block-occupying line in lines[lo:hi], else None."""
        for k in range(lo, hi):
            if _occupies_block(lines[k][2].strip()):
                return block_of(lines[k][0])
        return None

    def last_real_block(lo, hi):
        """Block of the last block-occupying line in lines[lo:hi], else None."""
        for k in range(hi - 1, lo - 1, -1):
            if _occupies_block(lines[k][2].strip()):
                return block_of(lines[k][0])
        return None

    def merge_after(close_idx):
        """The if's merge block, read LEXICALLY from the FIRST significant line after the
        if's closing brace. This is what makes the gate reorder-invariant for if/else — the
        merge is wherever it is EMITTED, not the address-next leader, so a BACKWARD
        (lower-address) shared merge resolves correctly. The cases:
          * a LABEL `L_M:`  -> M. A join is labelled (the stripped then-arm `goto L_M` and
            any backward edge reference it), and the label is the leader DIRECTLY — flush-safe,
            unlike block_of() on the first statement line, which a flushed-call's out-of-order
            address would misattribute (the $885E redraw-after-L_88D2 trap). Backward merges
            are ALWAYS labelled (you cannot fall backward, only goto), so this is exactly the
            case B needs.
          * an OPENER (`if/while/switch/do {`) -> its block (a construct header is a reliable
            leader; not subject to flush reordering).
          * a dedent (`}`, `} else {`, `} while`) -> None: the if ended its enclosing scope.
          * an unlabelled plain statement -> None: a forward/adjacent continuation, for which
            the caller's ADDRESS-based fallback is already correct (and flush-safe).
        Returns EXIT when nothing follows. Returning the FIRST significant token inherently
        cannot leak past an enclosing `}` (the 'leaks across scope' bug of the earlier crack):
        a dedent token short-circuits to None."""
        for k in range(close_idx + 1, len(lines)):
            t = lines[k][2].strip()
            if not t or t.startswith('//') or _RE_CASE_LABEL.match(t):
                continue                       # blanks / comments / case labels: skip
            if _RE_LABEL.match(t):
                return int(t[2:6], 16)         # join label = the merge leader (flush-safe)
            if t == '}' or t == '} else {' or _RE_DOWHILE_CLOSE.match(t):
                return None                    # dedent: if ended its enclosing scope
            if _opens_block(t):
                return block_of(lines[k][0])   # merge is a nested construct header
            return None                        # unlabelled plain continuation -> addr fallback
        return EXIT

    for header_addr, h_idx, else_idx, close_idx in ifs:
        # MERGE-ONLY lexical change (approach B, minimal): then_true / else_start stay
        # ADDRESS-based (next_leader — flush-safe and always-forward: the then-body is laid
        # right after the header). ONLY the MERGE is read lexically (merge_after, label-based)
        # so a BACKWARD shared merge resolves by its label instead of the address-next leader.
        hb = block_of(header_addr)
        # then-body entry: address-next (flush-safe) normally; but a SYNTHETIC then-body (an
        # un-shared duplicated cone, `if(c){ copy }`) sits at a synth addr, read lexically. Inert
        # otherwise (only a synth first-block overrides; a real then-body keeps the address entry).
        _then_end = else_idx if else_idx is not None else close_idx
        _then_fb = first_real_block(h_idx + 1, _then_end)
        then_true = _then_fb if (_then_fb is not None and _then_fb >= _SYNTH_BASE) else next_leader[hb]
        _ifcond = _RE_IF_OPEN.match(lines[h_idx][2].strip())
        _ifcond = _ifcond.group(1) if _ifcond else ''
        lex_merge = merge_after(close_idx)
        if else_idx is None:
            then_top = _span_max_leader(lines, h_idx + 1, close_idx, block_of)
            merge = lex_merge if lex_merge is not None else span_after(h_idx + 1, close_idx, hb)
            header_edges[hb] = {then_true, merge}
            _record_orient(orient, hb, _ifcond, then_true, merge)
            if then_top is not None and then_top != hb:
                tail_redirect[then_top] = merge        # then-tail -> merge (may be backward)
        else:
            then_top = _span_max_leader(lines, h_idx + 1, else_idx, block_of)
            # else-start, EMIT-ORDER (approach B): the FIRST block emitted after `} else {`, read
            # lexically. The address-based `next_leader[then_top]` is wrong when the else-arm is
            # internally address-inverted (its entry is a higher address than its body — e.g.
            # $AD38's else `AD7B` guard before its `AD67` body), where next_leader[then_top] lands
            # on the interior block. For an address-ordered arm the two agree (inert).
            else_start = first_real_block(else_idx + 1, close_idx)
            if else_start is None:
                else_start = next_leader[then_top] if then_top is not None else next_leader[hb]
            else_top = _span_max_leader(lines, else_idx + 1, close_idx, block_of)
            if lex_merge is not None:
                merge = lex_merge
            else:
                et = else_top if else_top is not None else else_start
                merge = next_leader[et]                # address fallback (former behaviour)
            header_edges[hb] = {then_true, else_start}
            _record_orient(orient, hb, _ifcond, then_true, else_start)
            # both arms' tails JUMP to the merge (structure_lines stripped their trailing
            # `goto L_merge`); the merge may be a lower address than the arm, so redirect via
            # the highest leader of each arm span (counts label-only / folded trailing blocks).
            if then_top is not None and then_top != hb:
                tail_redirect[then_top] = merge
            if else_top is not None and else_top != hb:
                tail_redirect[else_top] = merge

    loop_meta = []   # (span, header_block, exit_block, frozenset(body_leaders)) per loop

    def tail_falls_off(lo, hi):
        """True if the body's last statement can FALL off the bottom `}` (back to the
        loop top). An UNCONDITIONAL terminator (goto/return/break/continue, or a switch
        whose cases dispatch every path) cannot — its edge is already explicit, so the
        implicit body_tail->header back-edge must NOT be added (the menu-loop case: a
        tail `goto L_x` that targets a continue block would otherwise fabricate a
        spurious header edge). A conditional terminator (`if(C) goto/…`) or a plain
        statement DOES fall through, so the back-edge is kept."""
        for k in range(hi - 1, lo - 1, -1):
            t = lines[k][2].strip()
            if _is_scaffold(t):
                continue
            if _RE_SWITCH_CASE.match(t):
                return False
            cl, _ = _classify_stmt(t)
            return cl not in ('goto', 'return', 'break', 'continue')
        return True

    for header_addr, h_idx, close_idx in whiles:
        # POSITION-based (not next_leader): the loop emitter rotates the test to the
        # top, so the body's entry/tail are wherever the brace contents sit in the
        # listing — NOT necessarily the leader right after the header by address.
        #   while(C){body}: header -> { body_entry, exit }; body_tail -> header
        hb = block_of(header_addr)
        body_tail = last_real_block(h_idx + 1, close_idx)
        exit_blk = first_real_block(close_idx + 1, len(lines))
        if exit_blk is None:
            exit_blk = EXIT
        _wcond = _RE_WHILE_OPEN.match(lines[h_idx][2].strip())
        wcond = _wcond.group(1) if _wcond else ''
        if wcond == '1':
            # while(1) infinite loop: the opener adds NO condition edge — hb IS the loop
            # top, a normal body block (the switch / first statement) whose own edges the
            # per-block scan supplies. Register only the loop CONTEXT (break->exit,
            # continue->hb) and the fall-off back edge; hb stays in the body-leader set.
            bls = _loop_body_leaders(lines, h_idx, close_idx, block_of, leaders)
            if body_tail is not None and body_tail != hb and tail_falls_off(h_idx + 1, close_idx):
                back_edges.setdefault(body_tail, set()).add(hb)
                tail_fall_to_header[body_tail] = hb
            loop_meta.append((close_idx - h_idx, hb, exit_blk, frozenset(bls)))
            continue
        body_entry = first_real_block(h_idx + 1, close_idx)
        if body_entry is None:          # degenerate empty body: header just gates exit
            body_entry = exit_blk
        header_edges[hb] = {body_entry, exit_blk}
        _record_orient(orient, hb, wcond, body_entry, exit_blk)
        if body_tail is not None and body_tail != hb and tail_falls_off(h_idx + 1, close_idx):
            back_edges.setdefault(body_tail, set()).add(hb)   # body tail loops back
            tail_fall_to_header[body_tail] = hb
        # Body leaders (incl. label-only / folded ones): a rotated while hoists the
        # bottom test above the body, so any block OUTSIDE the loop whose address-next
        # leader lands INSIDE the body must instead enter via the header (the dropped
        # entry-guard `goto L_h` used to make that explicit). Recorded for the
        # fall-through fix below — keeps the address-based lowering otherwise intact.
        bls = _loop_body_leaders(lines, h_idx, close_idx, block_of, leaders)
        bls.discard(hb)
        while_bodies.append((hb, frozenset(bls)))
        loop_meta.append((close_idx - h_idx, hb, exit_blk, frozenset(bls)))

    for do_addr, _do_idx, close_addr, close_idx in dowhiles:
        # do { body } while (C);  — NO hoist (test stays at the bottom where it is).
        # The tail block (where the `} while(C);` test lives) branches back to the
        # header on continue, else falls to the block after the construct.
        hb = block_of(do_addr)
        ub = block_of(close_addr)
        exit_blk = first_real_block(close_idx + 1, len(lines))
        if exit_blk is None:
            exit_blk = EXIT
        dowhile_edges[ub] = {hb, exit_blk}
        _dcond = _RE_DOWHILE_CLOSE.match(lines[close_idx][2].strip())
        _record_orient(orient, ub, _dcond.group(1) if _dcond else '', hb, exit_blk)
        dbls = _loop_body_leaders(lines, _do_idx, close_idx, block_of, leaders)
        # KEEP the header block hb in the loop context (unlike the top-test `while`, whose
        # header is special-cased via header_edges). A two-test do-while puts its exit-test
        # `if (C) break;` INSIDE the header block; that block is lowered by the per-statement
        # scan, so it needs loop_ctx[hb] = (hb, exit) for the break to resolve to the loop
        # exit (else the header loses its exit edge -> CFG divergence). Harmless for plain
        # do-whiles (header has no break/continue) and for self-loops (hb == ub is special-
        # cased via dowhile_edges before the scan).
        loop_meta.append((close_idx - _do_idx, hb, exit_blk, frozenset(dbls)))

    # `break` resolves to the innermost enclosing loop OR SWITCH exit; `continue` skips
    # switches to the innermost LOOP header (C semantics). Build both maps, innermost (smallest
    # span) overwriting. switch_meta: (span, merge, body_leaders) — merge = the block after the
    # switch `}` where a `case …; break;` lands (label-aware via merge_after, so a pulled-out /
    # interleaved switch merge — atom 4 — resolves even when it's a lower address).
    switch_meta = []
    for sh_addr, sh_idx, sh_close in switches:
        sb = block_of(sh_addr)
        sm = merge_after(sh_close)
        if sm is None:
            sm = first_real_block(sh_close + 1, len(lines))
        if sm is None:
            sm = EXIT
        sbls = _loop_body_leaders(lines, sh_idx, sh_close, block_of, leaders)
        sbls.discard(sb)
        switch_meta.append((sh_close - sh_idx, sm, frozenset(sbls)))
    break_exit, continue_hdr = {}, {}
    constructs = ([('loop', sp, hb_, ex_, bl_) for sp, hb_, ex_, bl_ in loop_meta]
                  + [('switch', sp, None, mg_, bl_) for sp, mg_, bl_ in switch_meta])
    for typ, _sp, hb_, ex_, bl_ in sorted(constructs, key=lambda m: -m[1]):
        for L in bl_:
            break_exit[L] = ex_
            if typ == 'loop':
                continue_hdr[L] = hb_

    blocks = {L: [] for L in leaders}
    for addr, _ind, text in lines:
        t = text.strip()
        if _is_scaffold(t):
            continue
        blocks[block_of(addr)].append(t)

    cfg = {}
    for L in leaders:
        if L in switch_edges:
            # Inlined switch header: successors are the case targets (+ no-default fall),
            # not the per-block scan (whose only statement is the `switch (E) {` opener).
            cfg[L] = frozenset(switch_edges[L] | back_edges.get(L, set()))
            continue
        if L in header_edges:
            cfg[L] = frozenset(header_edges[L] | back_edges.get(L, set()))
            continue
        if L in dowhile_edges:
            cfg[L] = frozenset(dowhile_edges[L] | back_edges.get(L, set()))
            continue
        succ = None
        sw_targets, sw_has_default, is_switch = set(), False, False
        # The block's fall-through (not-taken / no-terminator) edge: the loop header if L
        # is a body tail, else the EMITTED-next block (lex_fall, label-anchored — approach B)
        # when the reducer reordered it, else the address-next leader. (goto/jmp TARGETS are
        # unaffected.)
        fall_thru = tail_fall_to_header.get(L, lex_fall.get(L, next_leader[L]))
        for t in blocks[L]:
            mc = _RE_SWITCH_CASE.match(t)
            if mc:
                is_switch = True
                sw_targets.add(int(mc.group(2), 16))
                if mc.group(1) == 'default':
                    sw_has_default = True
                continue
            cl, tgt = _classify_stmt(t)
            if cl == 'goto':
                succ = {block_of(tgt)}
            elif cl == 'ifgoto':
                succ = {block_of(tgt), fall_thru}
                mcnd = re.match(r'if \((.+)\) goto', t)
                _record_orient(orient, L, mcnd.group(1), block_of(tgt), fall_thru)
            elif cl in ('break', 'continue', 'ifbreak', 'ifcontinue'):
                is_brk = cl in ('break', 'ifbreak')
                jmp = break_exit.get(L) if is_brk else continue_hdr.get(L)
                if jmp is not None:
                    if cl in ('break', 'continue'):
                        succ = {jmp}
                    else:
                        succ = {jmp, fall_thru}
                        kw = 'break' if cl == 'ifbreak' else 'continue'
                        mcnd = re.match(rf'if \((.+)\) {kw}', t)
                        _record_orient(orient, L, mcnd.group(1), jmp, fall_thru)
            elif cl == 'return':
                succ = {EXIT}
            elif cl == 'ifreturn':
                succ = {EXIT, fall_thru}
                mcnd = re.match(r'if \((.+)\) return', t)
                _record_orient(orient, L, mcnd.group(1), EXIT, fall_thru)
        if is_switch:
            succ = {block_of(x) for x in sw_targets}
            if not sw_has_default:
                succ.add(fall_thru)
        if succ is None:
            # fall-through: if-else then-tail -> merge, else the (tail-aware) fall — but a
            # block falling into a HOISTED while body must enter via the header (you can't
            # fall into the middle of a rotated loop).
            fall = tail_redirect.get(L)
            if fall is None:
                fall = fall_thru
                for hb2, bls in while_bodies:
                    if fall in bls and L not in bls and L != hb2:
                        fall = hb2
                        break
            succ = {fall}
        cfg[L] = frozenset(succ | back_edges.get(L, set()))
    return cfg


def observable_blocks(lines, leaders):
    """Leaders whose block contains an observable side-effect statement (a store /
    call / assignment — anything that is NOT pure control or scaffold). These blocks
    are PRESERVED by contract(); routing-only and bare-`return` blocks are not, so
    they get bypassed. (A `return` alone is intentionally not "observable" — that is
    exactly what lets a bare return block fold into its branch, the early-return idiom.)"""
    block_of = _block_of_fn(leaders)
    obs = set()
    for addr, _ind, text in lines:
        t = text.strip()
        if _is_scaffold(t) or _RE_SWITCH_CASE.match(t):
            continue
        cl, _tgt = _classify_stmt(t)
        if cl in ('goto', 'ifgoto', 'return', 'ifreturn',
                  'break', 'continue', 'ifbreak', 'ifcontinue'):
            continue
        obs.add(block_of(addr))     # a plain statement = real side effect
    return obs


def contract(cfg, observable, entry, orient=None, crossjump=None):
    """Normalise a CFG by bypassing pure-routing blocks: any non-entry, non-observable
    block with a SINGLE successor (goto-only / empty fall-through / bare-`return`→EXIT)
    is removed and its predecessors rewired through it. Applied identically to both
    sides, this is the equivalence relation that ACCEPTS the behaviour-preserving folds
    structure_lines performs (early-return merges a return block; empty-arm if/else
    collapses a routing diamond) while still distinguishing any MISROUTE between
    observable (side-effecting) blocks. Returns the contracted CFG.

    `crossjump` (atom 5): a set of TERMINAL-SINK blocks (→EXIT) that are bypassed EVEN THOUGH
    observable. The decompiler inverts GCC's cross-jumping by DUPLICATING such a shared tail into
    each predecessor arm; the original and the duplicate are distinct blocks but carry the SAME
    content, so to compare the duplicated struct against the un-duplicated raw witness, BOTH sides
    bypass the sink (a →EXIT block routes nowhere — bypassing it preserves every arm's orientation
    toward EXIT). Restricted to ≥2-predecessor sinks by the caller: a 1-pred terminal arm (`return
    A` vs `return B`) must NOT be bypassed — collapsing those to EXIT would hide a then/else swap,
    the unsoundness the 114-suite catches. The copies==in-edges guard (vm_reduce) covers the
    content the routing-only bypass is blind to.

    If `orient` (a {cond_block: (taken, fall)} map of true/false edge labels) is given, it
    is rewired IN LOCKSTEP with the edges — each removed B->t bypass replaces B with t in
    every label too. Conditional blocks have 2 successors so are never themselves removed,
    so their labels survive and end up pointing at exactly the contracted graph's nodes.
    Doing this through contract() (not a separate resolver) is what keeps the orientation
    consistent with the contracted CFG on loop tails / mid-contraction self-loops."""
    g = {L: set(s) for L, s in cfg.items()}
    crossjump = crossjump or frozenset()
    changed = True
    while changed:
        changed = False
        for B in list(g):
            if B is EXIT or B == entry or (B in observable and B not in crossjump):
                continue
            succ = g.get(B)
            if succ is None or len(succ) != 1:
                continue
            (t,) = tuple(succ)
            if t == B:                          # self-loop: not a router
                continue
            for X in g:
                if B in g[X]:
                    g[X] = (g[X] - {B}) | {t}
            if orient is not None:
                for L, (tk, fl) in list(orient.items()):
                    if tk == B or fl == B:
                        orient[L] = (t if tk == B else tk, t if fl == B else fl)
            del g[B]
            changed = True
            break
    return {L: frozenset(s) for L, s in g.items()}


def structured_equivalent(raw_lines, struct_lines, leaders):
    """The structured-equivalence gate: True iff the STRUCTURED form has the
    same control flow as the RAW witness (= the bytecode, proven). Two checks:
      (1) contract()ed CFGs are equal — same routing between observable blocks; and
      (2) every conditional's TRUE/FALSE orientation agrees — so a then/else swap or a
          guard inversion (same two targets, wrong side) is caught, which the unlabelled
          edge SET in (1) would miss.
    Returns (ok, contracted_raw, contracted_struct) for reporting."""
    if not leaders:
        return True, {}, {}
    entry = leaders[0]
    or_raw, or_str = {}, {}
    cfg_raw = lower_goto_cfg(raw_lines, leaders, orient=or_raw)
    cfg_str = lower_struct_cfg(struct_lines, leaders, orient=or_str)
    # atom 5: cross-jump terminal sinks (→EXIT with ≥2 WITNESS preds) — bypassed on BOTH sides so a
    # DUPLICATED tail (struct) compares equal to the shared tail (raw). Computed from the raw witness
    # (the ground truth), so the set is identical for both contractions. The ≥2-pred restriction is
    # the soundness key (a 1-pred return arm stays distinct → then/else swaps still caught).
    _pred_n = {}
    for _u, _ss in cfg_raw.items():
        for _s in _ss:
            _pred_n[_s] = _pred_n.get(_s, 0) + 1
    # UN-SHARING bypass: (a) ≥2-pred TERMINAL sinks (cross-jump tails — atom 5); plus, when the struct
    # DUPLICATED a shared block, (b) every SYNTHETIC copy on the struct side AND (c) the real ORIGINAL
    # each copy was made from (decoded from its synth addr) — so the un-shared form (a copy in each
    # arm) compares equal to the shared witness. Each copy/original is single-successor (a tail →EXIT
    # or a continue-prologue →header), so contract bypasses it cleanly; the copies==in-edges guard
    # (vm_reduce) covers the per-arm content the routing-only bypass is blind to.
    _synth_copies = [T for T in cfg_str if T >= _SYNTH_BASE]
    crossjump = frozenset(
        [T for T, ss in cfg_raw.items() if ss == frozenset({EXIT}) and _pred_n.get(T, 0) >= 2]
        + _synth_copies
        + [_synth_origin(T) for T in _synth_copies])
    n_raw = contract(cfg_raw, observable_blocks(raw_lines, leaders), entry,
                     orient=or_raw, crossjump=crossjump)
    n_str = contract(cfg_str, observable_blocks(struct_lines, leaders), entry,
                     orient=or_str, crossjump=crossjump)
    if n_raw != n_str:
        return False, n_raw, n_str
    # true/false edge-label check: contract() rewired each conditional's (taken, fall) to
    # the contracted graph's nodes; drop the vacuous (taken == fall) ones and compare. A
    # then/else swap or guard inversion shows up here as a true/false target mismatch.
    fo_raw = {L: tf for L, tf in or_raw.items() if tf[0] != tf[1]}
    fo_str = {L: tf for L, tf in or_str.items() if tf[0] != tf[1]}
    return fo_raw == fo_str, n_raw, n_str


def _selftest(bank, sub_hex):
    """Eyeball one sub: print its bytecode CFG vs the lowered raw/structured CFGs."""
    import io
    from contextlib import redirect_stdout
    import tempfile
    from emu_harness.vm.koei.cli import decompile_all as dam
    from emu_harness.vm.koei import vm_decompile
    sub = int(sub_hex, 16)
    with tempfile.TemporaryDirectory() as td:
        captured = {}

        def on_sub(stub, collect):
            if stub == sub:
                captured.update(collect)
        dam.bank_subs(bank, Path(td), on_sub=on_sub)
    if not captured:
        print(f"sub ${sub:04X} not found in bank {bank}")
        return
    bc, leaders = bytecode_cfg(captured['instructions'])
    cc_raw = lower_goto_cfg(captured['raw'], leaders)
    cc_str = lower_struct_cfg(captured['structured'], leaders)
    ok, n_raw, n_str = structured_equivalent(captured['raw'], captured['structured'], leaders)

    def show(name, cfg):
        print(f"\n{name}:")
        for L in sorted(cfg, key=lambda x: (x is EXIT, x)):
            succ = ", ".join(s if s is EXIT else f"${s:04X}" for s in sorted(
                cfg[L], key=lambda x: (x is EXIT, x)))
            print(f"  ${L:04X} -> {{{succ}}}")
    show("bytecode_cfg", bc)
    show("lower(raw)", cc_raw)
    show("lower(structured)", cc_str)
    show("contracted raw", n_raw)
    show("contracted structured", n_str)
    print(f"\nraw == bytecode:            {cc_raw == bc}")
    print(f"structured ~= raw (contract): {ok}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3:
        _selftest(int(sys.argv[1]), sys.argv[2])
    else:
        print("usage: py -3 tools/vm_cfg.py <bank> <sub_hex>   (CFG self-test for one sub)")
