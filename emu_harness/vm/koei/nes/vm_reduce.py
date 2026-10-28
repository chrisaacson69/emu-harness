"""
tools/vm_reduce.py — REGION REDUCER (CFG epic V2 / "No More Gotos").

The single bottom-up CFG reducer that REPLACES the template menagerie
(`vm_decompile.structure_loops` + `structure_lines` + `structure_switches`).

WHY (the reframe, 2026-06-04 — see ROADMAP "CONTROL-FLOW STRUCTURING" epic +
[[project_nobunaga_decompiler_purpose]]): the gotos are assembly leftovers from a
structured-C source the compiler flattened. The job is to RECOVER that structure.
Template-matching cannot converge — it has no *closure under composition*, so every
new combination of shapes (a short-circuit feeding an if/else feeding a tail) looks
like a brand-new shape and spawns a new template. A region reducer DOES converge:
each rule firing collapses its subgraph into ONE abstract node that the next rule
consumes identically to a basic block, so a fixed grammar of ~7 rules yields
infinite nestings. RE-as-parsing — codegen walked DOWN the grammar; we build UP.

THE GRAMMAR (~7 fixed rules, applied to a FIXPOINT, bottom-up):
    sequence · if-then · if-then-else · short-circuit(&&/||) · while · do-while · switch
Irreducible residue (a goto INTO a block / multi-entry region) -> break one edge
with an honest goto and continue. Few, because most regions are reducible.

THE VERIFIER IS ALREADY BUILT — zero new verification work. The reducer proposes a
structured line list; `vm_cfg.structured_equivalent(raw, structured, leaders)` proves
it induces the SAME control-flow graph as the raw goto witness (== the bytecode) or
REJECTS it. Same safety model as the templates; the hard, valuable half is done.
Irreducibility is detected STRUCTURALLY by the reducer (no rule fires -> it breaks an
edge itself); the gate is a pure backstop that catches rule *bugs*, NOT the control
mechanism (inverts the template era, where gate-reject drove the fallback).

REUSE THE ANALYSIS, RE-DERIVE THE EMIT. The detectors in vm_cfg are *pure* and nail
the base shapes: `bytecode_cfg` (with value-diamonds/boolean-regions pre-contracted),
`dominators`/`post_dominators`, `back_edges`/`natural_loop`, `switch_dispatches`,
`boolean_regions`/`recover_bool_formula`. The reducer's new code is a region-tree
builder over that CFG + a tree -> (addr, indent, text) pretty-printer whose line
format matches what `structured_equivalent` consumes (so the gate reads it unchanged).

BUILD LADDER (each rung lands gate-green + measured on tools/v2-corpus.py):
    0. [DONE] harness + passthrough — reduce() returns lines unchanged; corpus wired.
    1. [DONE] block model + trivial emit — partition lines into blocks; emit seq-of-blocks
       with gotos (reproduce the raw form FROM the tree) -> proves block<->line round-trip.
    2. [DONE] sequence + if-then + if-then-else (acyclic diamonds via dom/post-dom).
    4. [DONE] while / do-while / while(1) (natural loops via back_edges / natural_loop) +
       early-return INLINING (bare-return targets become `if(C) return X;`, not a shared
       merge). 171 subs fold / 0 gate-reject. break/continue/return are loop-terminal arms;
       in-body merges use a body-LOCAL post-dom; rotated for-loops lift only the test expr.
       Conservative bails (all detected STRUCTURALLY, never gate-reject): nested loops (4d),
       compound multi-exit-test loops (need rung 3), a loop whose exit is non-adjacent or a
       pure-routing block (tail-of-if-arm), an empty then-arm with a goto-side body.
    3. short-circuit (&&/||) — DROPPED as a rung: the gate compares per-conditional-block
       orientation and contract() never removes a 2-succ block, so a merged `if(a&&b) goto`
       loses a node and is REJECTED; V1 itself emits NESTED ifs, not `&&`. No short_circuit
       bail exists in --bails. (Those cases surface as compound_pretest / multi_exit_loop.)
    cross_edge fold (DONE 2026-06-04, part of the acyclic walk, not a numbered rung): a forward
       branch to a SHARED continuation where the fall arm also reaches it but post-dom is EXIT
       (an early return on one path). `_reaches(fall, goto, n)` detects it -> use the goto-target
       as the merge so the tail is structured ONCE (the early returns become terminal guards).
       Inlined return blocks count for merge-ADJACENCY (added to `used`) but are EXCLUDED from
       the arm-ORDER disjointness check (a shared tail return floats). $9850/$A553 -> 0 gotos,
       beating V1 (which left honest gotos). behind 130->127, gotos 1541->1498, 0 gate-rejects.
    5. [DONE 2026-06-04] switch — block-level port of structure_switches as a composable region
       node (_structure_switch): keep the `switch(E){` opener, drop the `case N: goto` dispatch
       + closer, inline each case-target block's RAW lines (gotos verbatim — no break-conversion
       / no intra-case folding yet) in address order with `case N:`/`default:` labels, close
       after the last enclosed block. Folds the switch AND its enclosing loop/if (the closure
       payoff — e.g. $8D5D switch-in-while, $9BB4 fall-through ladder, both 0 gotos). +12 subs
       (171->183), V2 gotos 1624->1541, behind 142->130, 0 gate-rejects. An UN-foldable switch
       (not single-entry) still bails the sub (reason 'switch_unfoldable', 9 subs). The bigger
       switch potential (e.g. $A2D2) is gated behind a co-occurring cross_edge bail (whole-sub
       fallback hides the fold) — see the bail histogram. Follow-ups: break-conversion to the
       merge, and intra-case structuring (recurse _structure inside a case body) to match V1
       on switch subs whose cases carry ifs.
    4d. nested loops — drop the `any header inside loop -> bail` guard once 3/5 reduce noise.
    6. irreducible fallback — break one retreating/cross edge, honest goto, continue.
    7. cutover — V2 folds >= templates across all 495, every gate green -> flip default
       on, regen decompiled/*.c, DELETE the template structure_* functions.

reduce(lines, instructions) -> structured (addr, indent, text) line list.
    `lines` = the raw goto-form leaves the expression layer already emits
    (vm_decompile State.lines). `instructions` = the decoded bytecode for this sub.

RUNG 0: passthrough. Returns `lines` unchanged (== the raw goto witness, which the
gate accepts trivially since lower(raw) == bytecode by construction). Every sub
therefore reports as honest-goto / zero-fold; the templates fold more. That gap is
exactly what rungs 1-6 close. The harness, the gate wiring, and the corpus oracle
are proven FIRST, before any rule can introduce a bug.
"""


import re
from collections import defaultdict

from emu_harness.vm.koei.nes import vm_cfg


# === Rung 1: block model + trivial emit ========================================
# Partition the raw (addr, indent, text) leaves into basic blocks by CFG leader, then
# emit the blocks in leader (address) order. This reproduces the raw goto/label form
# FROM a block-structured tree — proving the block<->line round-trip is faithful
# BEFORE any folding rule can perturb it. The one known hazard is the decompiler's
# flushed-call reordering (a discarded side-effecting CALL is appended at its later
# FLUSH point but tagged with its earlier call-site addr; vm_decompile.State.flush_
# pending) — partitioning by block_of(addr) would move such a line earlier, changing
# WHERE the side effect lands. So a leaf preserves its lines in RAW-LIST ORDER and the
# emit is checked against raw (the corpus "honest-goto" metric == structured-equals-raw):
# any sub that diverges is a reordering to handle, not a silent behaviour change.


def _partition(lines, leaders):
    """Group the raw leaves into {leader: [(addr, indent, text), ...]} by block_of(addr),
    preserving each line's original list order within its block (so a flushed-call line
    keeps its relative position among its block-mates)."""
    block_of = vm_cfg._block_of_fn(leaders)
    buckets = {L: [] for L in leaders}
    for addr, ind, text in lines:
        buckets[block_of(addr)].append((addr, ind, text))
    return buckets


def _flat_emit(buckets, leaders):
    """Rung-1 fallback: blocks in leader order, goto/label form intact."""
    out = []
    for L in leaders:
        out.extend(buckets[L])
    return out


# === Rungs 2 + 4: sequence / if-then / if-then-else / while / do-while / while(1) =
# The single recursive structurer. `_structure` walks a single-entry region into a tree of
# Seq / IfThen / IfThenElse, using the post-dominator as each 2-way branch's merge point.
# Branch polarity mirrors the decompiler exactly: a header's raw line is `if (RAWC) goto L_T`
# (goto = TRUE arm, fall-through = the body), so the structured then-body is the FALL-THROUGH
# arm with condition `_neg(RAWC)` — the same Pattern-A/B inversion, and the print loop's
# simplify_expression cleans `!((a<b))` -> `(a>=b)`.
#
# RUNG 4 — loops compose into the SAME recursion. A natural loop (back edge u->h, h dom u)
# collapses to ONE AST node (`_structure_loop`), consumed by the surrounding walk exactly
# like a basic block — so acyclic if/then/else (rung 2) nests inside loop bodies AND wraps
# around loops for free. Form is read from the CFG (reuse the analysis):
#   * pre-test  while(C){body}      header h is the 2-way test: one succ in loop, one = exit
#   * post-test do{body}while(C)    a single latch's 2-way test gates the back edge
#   * endless   while(1){body}      no governing 2-way test; control leaves via break/return
# A pre-test header that ALSO carries statements before its test renders faithfully as
# while(1){ stmts; if(!C) break; body } (no statement reorder — the honest form). Inside a
# loop body an edge to the header is `continue`, an edge to the exit is `break`; the if/then
# builder treats those as terminal arms. In-body merges use a body-LOCAL post-dominator (the
# global pdom is wrong — a breaking arm skips the merge), computed over the loop nodes with
# the header/exit/outside edges dropped so non-breaking paths' reconvergence is what shows.
#
# CONSERVATIVE by design: any shape a rung doesn't cleanly own (a block reached twice = cross
# edge, a forward goto skipping the merge, a switch, a multi-exit loop, nested loops [4d],
# a body that would force a statement reorder) raises NotReducible and the whole sub falls
# back to the rung-1 flat emit. The CFG-equivalence gate (structured_equivalent, applied in
# vm_decompile.decompile) is the backstop: a tree that builds but mis-routes is rejected
# there and the sub reverts to honest goto — never corrupted output.

# A statement line to emit verbatim, vs control scaffolding the structure consumes.
_RE_IFGOTO = re.compile(r'if \((.+)\) goto L_([0-9A-Fa-f]{4});$')
_RE_GOTO = re.compile(r'goto L_([0-9A-Fa-f]{4});$')


class _NotReducible(Exception):
    """Raised by a structuring rung when it can't cleanly own a shape (the whole sub then
    falls back to the rung-1 flat emit). `reason` is a short category tag for the bail
    histogram (`v2-corpus.py --bails`) — diagnostic only, never alters behaviour."""
    def __init__(self, reason=None):
        super().__init__(reason)
        self.reason = reason


# Bail attribution: the reason the LAST reduce() call fell back to flat emit (None if it
# folded cleanly). Single-threaded, set synchronously inside reduce() — read it right after.
# Drives `v2-corpus.py --bails`, which ranks the residue so the next rung targets the biggest
# lever. Purely diagnostic; does NOT affect emitted lines.
LAST_BAIL = None


# Audit harness (atom-log discipline): a set of bail reasons to SUPPRESS, so we can test whether
# a guard is a genuine soundness invariant or an untested "the gate can't handle this" guess (the
# atom-3 lesson). `_audit_bail(reason)` raises normally UNLESS the reason is suppressed, in which
# case it falls through and the caller builds the node anyway — reduce()'s self-validation is the
# backstop, so a wrong guess shows up as a gate-reject, a correct removal as a fold. Default empty
# (production behaviour identical). Only used at guards whose fall-through builds a CFG-validatable
# node (NOT semantic guards like compound_pretest, which the CFG gate can't police).
_AUDIT_SKIP = set()


def _audit_bail(reason):
    if reason not in _AUDIT_SKIP:
        raise _NotReducible(reason)


def _parse_block(block_lines):
    """(stmts, rawcond, branch_addr, goto_target, is_switch) for one block's raw lines.
    stmts = [(addr, text)] to emit verbatim (the block body incl. any `return …;`); the
    label and the goto/if-goto terminator are stripped (the structure re-supplies them)."""
    stmts, rawcond, branch_addr, goto_target, is_switch = [], None, None, None, False
    for addr, _ind, text in block_lines:
        t = text.strip()
        if vm_cfg._RE_LABEL.match(t):
            continue
        m = _RE_IFGOTO.match(t)
        if m:
            rawcond, goto_target, branch_addr = m.group(1), int(m.group(2), 16), addr
            continue
        if _RE_GOTO.match(t):
            branch_addr = addr
            continue
        if vm_cfg._RE_SWITCH_CASE.match(t):
            is_switch = True
            continue
        stmts.append((addr, t))
    return stmts, rawcond, branch_addr, goto_target, is_switch


_STOP = object()          # "no merge stop" sentinel: a loop body walk ends only on h / exit


class _Ctx:
    """Per-sub invariants shared across the whole structuring recursion.
      full_cfg : the sub's block CFG (real successors, never remapped)
      info     : {leader: _parse_block(...)} — stmts / rawcond / branch_addr / goto_target
      block_of : addr -> its block leader;  nxt : leader -> address-next leader (or EXIT)
      loops    : {header: frozenset(loop nodes)} ;  latches : {header: set(latch blocks)}
      buckets  : {leader: [(addr,indent,text)]} — the RAW partitioned lines (rung 5 reads
                 the verbatim case-body lines from here, gotos/labels intact)
      switches : {switch_block: dispatch dict} — foldable switches (vm_cfg.switch_dispatches)
      visited  : cross-edge guard (a block reached twice = irreducible)
      far_by_loop : {header: frozenset(blocks)} — multi-LEVEL break targets for that loop: an
                 exit that escapes the PARENT loop too (C's single-level break can't reach it),
                 so the edge is kept as an honest `goto L_t` inside the body instead of a
                 structured break, and excluded from the loop's exit set (_structure_loop)."""
    __slots__ = ("full_cfg", "info", "block_of", "nxt", "loops", "latches",
                 "buckets", "switches", "visited", "far_by_loop", "sink_merge", "merge_nonadj",
                 "dup_terminals", "preds", "dup_count", "synth_next")

    def __init__(self, full_cfg, info, block_of, nxt, loops, latches, buckets, switches):
        self.full_cfg, self.info, self.block_of, self.nxt = full_cfg, info, block_of, nxt
        self.loops, self.latches = loops, latches
        self.buckets, self.switches, self.visited = buckets, switches, set()
        self.far_by_loop = {}
        # atom 5 — cross-jump terminal DUPLICATION. OFF by default; reduce() trials it on/off and
        # keeps the fewest-goto validating fold. preds = block -> set of CFG predecessors (the
        # WITNESS in-edges, used both to gate the dup to genuine >=2-pred shared sinks and for the
        # copies==in-edges content guard). dup_count = sink -> number of DUPLICATE copies emitted.
        self.dup_terminals = False
        self.preds = defaultdict(set)
        for _u, _ss in full_cfg.items():
            for _s in _ss:
                self.preds[_s].add(_u)
        self.dup_count = defaultdict(int)
        self.synth_next = defaultdict(int)   # original-block -> next copy index (synth addr alloc)
        # Track A (family-1) sink-as-merge atom; OFF by default. reduce() runs the structuring
        # both ways and keeps whichever validates with FEWER gotos (goto-count-aware acceptance),
        # so the atom never regresses a sub that folds better without it (e.g. $A853).
        self.sink_merge = False
        # Track B (atom 8) non-adjacent-merge fold; OFF by default. Under the flag, an if-node is
        # built even when its merge is NOT the address-next leader (the merge_not_adjacent bail is
        # suppressed) — recovering the 77%-majority forward shared-merge gotos. Same goto-count-
        # aware acceptance over the _COMBOS grid guards it; the gate rejects any bad layout.
        self.merge_nonadj = False


def _real_stmts(stmts):
    """Block stmts minus cosmetic COMMENT lines (`// ext_op …` provenance the decompiler emits
    for 32-bit math helpers). Comments carry no control and no effect, so they must not inflate a
    block's statement count — else a `return X;` block padded with an ext_op nop comment is read as
    multi-statement and the early-return inline ($8327's $8354) misses it."""
    return [s for s in stmts if not s[1].lstrip().startswith('//')]


def _is_return_block(L, ctx):
    """A leaf that is just `return …;` (-> EXIT). Such a block is INLINED at each jump to it
    (the early-return idiom: `if (C) return X;`) instead of being a shared merge — which is
    both how the C reads and CFG-neutral (contract() bypasses bare-return blocks anyway), so
    several branches converging on one `return` don't force an irreducible shared tail."""
    stmts, rawcond, _b, _g, sw = ctx.info[L]
    real = _real_stmts(stmts)
    return (rawcond is None and not sw and len(real) == 1
            and real[0][1].startswith('return')
            and ctx.full_cfg[L] == frozenset({vm_cfg.EXIT}))


def _dupable_prologue(b, loop, ctx):
    """A bounded shared block reached via a guard that should be UN-SHARED (duplicated into the guard
    arm) rather than reached by `goto`. Currently a CONTINUE-PROLOGUE: a single-successor content
    block (the loop's increment/update) whose successor IS the loop header, with ≥2 preds — the
    `$9C22`/`rot_incr_break` shape where several continue-guards funnel through one shared update
    block. Returns its stmts (to emit as `if(c){ stmts; continue; }`), else None. The compiler shared
    this update across the guards; un-sharing puts the boundary back so each guard composes cleanly."""
    if loop is None or b is vm_cfg.EXIT:
        return None
    h, _e = loop
    succ = ctx.full_cfg.get(b)
    if not succ or len(succ) != 1 or next(iter(succ)) != h:   # single-succ -> the loop header
        return None
    if len(ctx.preds[b]) < 2:
        return None
    stmts, rawcond, _ba, _gt, sw = ctx.info[b]
    if sw or rawcond is not None or not _real_stmts(stmts):    # plain content block, no branch
        return None
    return stmts


def _has_effect(seq):
    """True if `seq` emits any observable line — a non-empty basic block or any construct
    (if / guard / term / loop). An effect-LESS sequence is pure routing (empty blocks a value
    -diamond left behind), which the address-based gate can't place as an if-arm."""
    for it in seq:
        if it[0] == 'block':
            if it[2]:
                return True
        else:
            return True
    return False


def _reaches(start, target, avoid, ctx):
    """True if `target` is reachable from `start` in the block CFG without passing back through
    `avoid` (the branch header). Pure — never touches ctx.visited. Used to tell a forward
    branch to a SHARED continuation (the fall arm reaches the goto-target too) from a true
    if-then-else (the goto-target is an else-region the fall arm never reaches)."""
    seen = {avoid}
    stack = [start]
    while stack:
        b = stack.pop()
        if b is vm_cfg.EXIT or b in seen:
            continue
        if b == target:
            return True
        seen.add(b)
        stack.extend(ctx.full_cfg[b])
    return False


def _reachable_avoiding(start, avoid_set, ctx):
    """The set of blocks reachable from `start` WITHOUT entering any block in `avoid_set` (the
    loop), `start` included. Used to find where several loop-exit targets CONVERGE."""
    seen, stack = set(), [start]
    while stack:
        b = stack.pop()
        if b is vm_cfg.EXIT or b in seen or b in avoid_set:
            continue
        seen.add(b)
        stack.extend(ctx.full_cfg[b])
    return seen


def _convergent_exit(targets, loop, ctx):
    """A loop with several immediate exit targets is still SINGLE-exit if they CONVERGE on one
    block `c` (outside the loop) that EVERY target reaches without re-entering the loop — then
    `c` is the true exit and the non-`c` targets are short 'exit shims' pulled into the loop
    body as in-body break paths. `c` may be one of the targets (e.g. $8C45's `8C6D`, reached
    directly and via the `local11=1` shim `8C61`) or a common successor of all of them (e.g.
    $ADF6's `AE77`, where BOTH exits flow to it but neither IS it). Return `c`, else None
    (exits genuinely diverge). SOUNDNESS: every shim (a target that isn't `c`) must be
    loop-PRIVATE (no predecessor outside the loop), else folding it into the body would
    relocate code reached from elsewhere. Prefer the closest convergence (lowest address)."""
    preds = {}
    for b, succs in ctx.full_cfg.items():
        for s in succs:
            preds.setdefault(s, set()).add(b)
    reach = {t: _reachable_avoiding(t, loop, ctx) for t in targets}
    common = set.intersection(*reach.values()) if reach else set()
    # candidate exits: a target all others reach, or any common successor; closest first.
    for c in sorted(common, key=lambda x: (x is vm_cfg.EXIT, x)):
        if c in loop:
            continue
        if all(preds.get(t, set()) <= loop for t in targets if t != c):
            return c
    return None


def _terminal(b, loop, ctx):
    """If a control edge to block `b` should render as a one-line terminator rather than a
    walk INTO b, return that statement (no trailing `;`): `continue`/`break` for the
    enclosing loop's header/exit, or the inlined `return …` of a bare-return target. Else
    None (b is ordinary region code to keep structuring)."""
    if loop is not None:
        h, e = loop
        if b == h:
            return 'continue'
        if e is not None and b == e:
            return 'break'
        # Multi-LEVEL break: an edge out of this loop to a target that ALSO escapes the parent
        # loop (C's break only leaves ONE loop) -> kept as an honest `goto L_t` (the `goto
        # DISPLAY`/`goto done` abort idiom). The e-check above runs first, so the loop's OWN
        # single-level exit still renders as a clean break; only the farther target gotos.
        if b is not vm_cfg.EXIT and b in ctx.far_by_loop.get(h, ()):
            return f'goto L_{b:04X}'
    if b is not vm_cfg.EXIT and _is_return_block(b, ctx):
        return _real_stmts(ctx.info[b][0])[0][1].rstrip(';')   # the return stmt (skip nop comments)
    return None


def _structure(entry, stop, pdom, ctx, loop):
    """Structure the single-entry region from `entry` until control reaches `stop` (a merge
    block), EXIT, or — inside a loop — the header (continue/loop-back) or the exit (break).
    `pdom` is the CURRENT region's post-dominators (the sub's at top level, a body-local one
    inside a loop). `loop` = (header, exit) or None. Returns (seq, used): seq is a list of
    AST items, used the set of block leaders consumed.
      ('block', leader, [(addr,text)…])                       — a basic block's body
      ('if', branch_addr, [(addr,text)…], cond, then, else)   — else=[] for an if-then
      ('guard', branch_addr, cond, kind)                      — `if (cond) break;/continue;`
      ('term', kind)                                          — bare `break;`/`continue;`
      ('loop', form, header_addr, cond, body, latch_addr, latch_stmts)  — a folded loop
    Raises _NotReducible on any shape no rung cleanly owns."""
    seq, used = [], set()
    n = entry
    while n is not vm_cfg.EXIT and n != stop:
        # a loop header reached from outside -> reduce the whole loop to one node.
        if n in ctx.loops and (loop is None or n != loop[0]):
            node, consumed, after = _structure_loop(n, ctx, loop)
            seq.append(node)
            used |= consumed
            n = after
            continue
        # a foldable switch dispatch head -> fold the whole switch territory to one node (rung 5)
        if n in ctx.switches and n not in ctx.visited:
            node, consumed, after = _structure_switch(n, ctx, loop)
            seq.append(node)
            used |= consumed
            ctx.visited |= consumed
            n = after
            continue
        if n in ctx.visited:
            raise _NotReducible('cross_edge_top')    # cross / shared edge (top-of-walk)
        ctx.visited.add(n)
        used.add(n)
        stmts, rawcond, baddr, gtgt, _sw = ctx.info[n]
        succ = ctx.full_cfg[n]
        # RUNG 6 — un-foldable switch local fallback. A switch dispatch the detector can't fold
        # to a single-entry region (not in ctx.switches) is kept as an honest flat span — but it
        # must span the WHOLE switch TERRITORY (dispatch + every case body), not just the
        # dispatch block: the case bodies are reached ONLY via the dispatch gotos, so a linear
        # walk would ORPHAN every case except the first fall-through chain. Territory = all
        # blocks reachable from the dispatch up to its post-dominator merge (where the cases
        # reconverge); flat-span it (raw gotos verbatim) and continue at the merge. The sub's
        # code AROUND the switch still folds — localizes the old whole-sub `switch_unfoldable`
        # bail. (Matching V1's intra-case structuring is a separate rung-5 follow-up.)
        if _sw and n not in ctx.switches:
            merge = vm_cfg._imm_post_dom(pdom, n)
            avoid = set() if merge is vm_cfg.EXIT else {merge}
            territory = {b for b in _reachable_avoiding(n, avoid, ctx) if b is not vm_cfg.EXIT}
            # Continue the walk AT the merge only when it cleanly follows the whole switch by
            # address AND structuring the post-switch region is safe. Two unsafe shapes -> span
            # the WHOLE forward cone instead (merge = EXIT, no continuation): (a) a case body
            # WRAPS past the merge (a higher address — continuing there reverses address order
            # and the gate mis-routes, e.g. $D14E's $D270 past merge $D269); (b) the merge has
            # an out-of-territory predecessor (it's a shared continuation reached from elsewhere,
            # e.g. $D14E's $D269 with 4 preds — feeding it to the linear walk hits the cross-edge
            # structurer which mis-folds). Whole-cone keeps the switch territory honest-goto and
            # still folds the code BEFORE the switch.
            preds_of_merge = {b for b in ctx.full_cfg if merge in ctx.full_cfg[b]}
            if merge is not vm_cfg.EXIT and (any(b >= merge for b in territory)
                                             or not preds_of_merge <= territory):
                merge = vm_cfg.EXIT
                territory = {b for b in _reachable_avoiding(n, set(), ctx) if b is not vm_cfg.EXIT}
            node, consumed, after = _flat_span(territory, ctx, merge)
            seq.append(node)
            used |= consumed
            n = after
            continue
        if len(succ) == 1:
            (s,) = tuple(succ)
            seq.append(('block', n, stmts))
            st = _terminal(s, loop, ctx)             # continue / break / inlined return
            if st is not None:
                seq.append(('term', st, n))          # addr = this block (gate keys by addr)
                break
            if s is not vm_cfg.EXIT and s != stop and s in ctx.visited:
                # RUNG 6 — cross-edge local cut. This strand falls into an already-EMITTED
                # block s (a shared continuation reached by 2+ paths — the classic multiple
                # `goto cleanup` / `goto done` idiom, or a shared `return` block like $AD38's
                # $AD58). Keep THIS edge as an honest `goto L_s` (block n's terminator, tagged
                # with n's addr so the gate attributes the n->s edge to block n) and stop the
                # strand — s and its cone were already structured on the path that first visited
                # it. Both FORWARD and BACKWARD cuts are allowed: reduce() self-validates, so a
                # cut that reverses lexical-vs-address order self-gates cleanly (reverts to flat
                # / region-fallback) while one that holds is kept. CFG-faithful by construction.
                #
                # ATOM 5 — cross-jump terminal DUPLICATION. When s is a TERMINAL SINK (-> EXIT) with
                # >=2 witness preds (the genuine cross-jumped shared tail, e.g. $AD38's $AD58), the
                # honest inverse of GCC's cross-jump is to DUPLICATE s's content into THIS arm rather
                # than goto it — recovering the -O0 form (each arm ends in its own copy of the tail).
                # The copy is re-tagged to n's block (block_of -> n) so the gate sees n -> EXIT (not a
                # shared AD58), while the FIRST predecessor keeps the original copy. The contract()
                # >=2-pred terminal-sink bypass (structured_equivalent) makes both sides agree; the
                # copies==in-edges guard (reduce) is the content backstop the routing gate is blind to.
                if (ctx.dup_terminals and ctx.full_cfg[s] == frozenset({vm_cfg.EXIT})
                        and len(ctx.preds[s]) >= 2 and not ctx.info[s][4]):   # not a switch block
                    seq.append(('dup', n, s, ctx.info[s][0]))   # ('dup', host, sink, sink_stmts)
                    ctx.dup_count[s] += 1
                    break
                seq.append(('goto', n, s))
                break
            n = s
        elif len(succ) == 2 and rawcond is not None and gtgt is not None:
            goto_block = ctx.block_of(gtgt)
            if goto_block not in succ:
                raise _NotReducible('goto_not_succ')
            fall = next((x for x in succ if x != goto_block), None)
            if fall is None:
                raise _NotReducible('no_fall')
            gt, ft = _terminal(goto_block, loop, ctx), _terminal(fall, loop, ctx)
            # --- single-sided guard: `if (cond) break;/continue;/return X;` ---------------
            if gt is not None and ft is None:
                seq.append(('block', n, stmts))
                seq.append(('guard', baddr, rawcond, gt))        # taken (rawcond) -> terminal
                if goto_block is not vm_cfg.EXIT and _is_return_block(goto_block, ctx):
                    used.add(goto_block)         # inlined return occupies its address span
                n = fall
                continue
            if ft is not None and gt is None:
                seq.append(('block', n, stmts))
                seq.append(('guard', baddr, vm_cfg._neg(rawcond), ft))   # !rawcond -> terminal
                if fall is not vm_cfg.EXIT and _is_return_block(fall, ctx):
                    used.add(fall)               # inlined return occupies its address span
                n = goto_block
                continue
            # --- UN-SHARING: a guard to a bounded shared CONTINUE-PROLOGUE (a loop's shared update
            # block several guards funnel through). Duplicate it into the guard arm — `if(c){ incr();
            # continue; }` — so the boundary the compiler shared is put back and the body composes.
            # The copy is emitted at a synthetic addr so N copies don't collide; the gate bypasses
            # copies + the original; the copies==in-edges guard covers content.
            if ctx.dup_terminals and gt is None and ft is None:
                gp, fp = _dupable_prologue(goto_block, loop, ctx), _dupable_prologue(fall, loop, ctx)
                if gp is not None and fp is None:
                    seq.append(('block', n, stmts))
                    s_addr = vm_cfg._SYNTH_BASE + (goto_block << 8) + ctx.synth_next[goto_block]
                    ctx.synth_next[goto_block] += 1
                    seq.append(('ifdup', baddr, rawcond, goto_block, s_addr, gp, 'continue'))
                    ctx.dup_count[goto_block] += 1
                    n = fall
                    continue
                if fp is not None and gp is None:
                    seq.append(('block', n, stmts))
                    s_addr = vm_cfg._SYNTH_BASE + (fall << 8) + ctx.synth_next[fall]
                    ctx.synth_next[fall] += 1
                    seq.append(('ifdup', baddr, vm_cfg._neg(rawcond), fall, s_addr, fp, 'continue'))
                    ctx.dup_count[fall] += 1
                    n = goto_block
                    continue
            if gt is not None and ft is not None:
                # both arms leave the region. This is the body TAIL (we break the walk after),
                # so an arm that `continue`s is just the implicit fall-off back to the header —
                # collapse it into a SINGLE guard on the OTHER arm (`if(cond) <other>;`), avoiding
                # a separate term tagged with the fall block's addr (which would COLLIDE when that
                # block is also emitted elsewhere — e.g. a multi-level break target). Else emit
                # the generic guard + tail term (guard keeps the branch addr, term its block addr).
                seq.append(('block', n, stmts))
                if gt == 'continue':
                    seq.append(('guard', baddr, vm_cfg._neg(rawcond), ft))   # !rawcond -> ft
                elif ft == 'continue':
                    seq.append(('guard', baddr, rawcond, gt))                # rawcond -> gt
                else:
                    seq.append(('guard', baddr, rawcond, gt))
                    seq.append(('term', ft, fall))
                break
            # --- both arms are regions: if-then / if-then-else (rung 2) -------------------
            # RUNG 6 — local honest-goto fallback. The clean if/then/else recovery below
            # raises _NotReducible on any shape it can't own (a shared continuation reached by
            # both arms = cross_edge, a non-adjacent merge, a goto past the merge, an empty
            # then-arm, reversed arm order…). Instead of flattening the WHOLE sub, catch it
            # here and emit the branch as an honest `if (rawcond) goto L_t;` — exactly the raw
            # conditional, so its CFG edge {goto_block, fall} is reproduced verbatim (sound by
            # construction, never a gate-reject) — then continue the linear walk at `fall`. The
            # surrounding region keeps all the structure it can; only this one edge stays a
            # goto, matching what V1's templates leave (e.g. $999A's tax-cap shared join). The
            # arm walks mutate ctx.visited as they probe; snapshot + restore it so the blocks
            # they touched are re-walked (and properly emitted) on the linear continuation.
            saved_visited = set(ctx.visited)
            saved_dup = dict(ctx.dup_count)    # arm probes may fire un-sharing dups; roll back on bail
            try:
                if fall is vm_cfg.EXIT:
                    raise _NotReducible('header_falls_off')  # header falls off the end — rare
                merge = vm_cfg._imm_post_dom(pdom, n)
                # Early-return shared continuation: no clean post-dom merge (EXIT) because the
                # fall arm RETURNS on some path, yet the branch's goto-target is itself reached
                # BY the fall arm -> that target is the real reconvergence. Use it as the merge
                # so the early returns fold to terminal guards inside the then-arm and the tail
                # is structured ONCE after the if (else it is walked in BOTH arms -> cross_edge).
                if merge is vm_cfg.EXIT and goto_block is not vm_cfg.EXIT \
                        and _reaches(fall, goto_block, n, ctx):
                    merge = goto_block
                # ATOM (Track A, family-1) — branch to a SHARED TERMINAL sink. When no local
                # post-dom was found (imm_post_dom = EXIT, because the fall arm RETURNS on some path
                # before the join) but the goto-arm targets a sink block (-> EXIT, e.g. a multi-
                # statement shared `return` tail), that sink IS the region's merge: the goto-arm is
                # empty and the sink folds ONCE after the if. Without this the shared terminal gets
                # CONSUMED as this branch's (or an inner branch's) else-arm, making max(consumed) ==
                # the tail so `nxt[tail] != merge` trips merge_not_adjacent -> an honest `goto
                # L_tail`. Sound + reducer-only when the sink is address-last (a FORWARD merge); when
                # it is interleaved (an address-inverted predecessor, family-2) the adjacency check
                # below still bails to goto. reduce()'s self-validation is the backstop either way.
                # ($9F04, and the forward-sink slice of the residue.)
                if ctx.sink_merge and merge is vm_cfg.EXIT and goto_block is not vm_cfg.EXIT \
                        and ctx.full_cfg[goto_block] == frozenset({vm_cfg.EXIT}):
                    merge = goto_block
                # The goto target must stay within the region (<= merge by address); a goto PAST
                # the merge is a forward skip (break-like) -> irreducible here.
                if merge is not vm_cfg.EXIT and goto_block > merge:
                    _audit_bail('goto_past_merge')
                then_seq, then_used = ([], set()) if fall == merge else \
                    _structure(fall, merge, pdom, ctx, loop)
                else_seq, else_used = ([], set()) if goto_block == merge else \
                    _structure(goto_block, merge, pdom, ctx, loop)
                if not _has_effect(then_seq) and _has_effect(else_seq):
                    # The then-arm is empty but the else-arm has a real body: the body lives on
                    # the GOTO side (`if(C) goto body; goto merge`), often with a routing block
                    # laid out BETWEEN the header and that body. The gate finds the else boundary
                    # by address (next leader after the then span), which an empty then-arm makes
                    # ambiguous -> bail. (Both arms empty = a value-diamond shell — fold it.)
                    _audit_bail('empty_then')
                if else_seq:
                    if not (fall < goto_block):
                        _audit_bail('else_before_then')  # then(fall) must precede else(goto)
                    # The ENTIRE then-region must precede the ENTIRE else-region by address —
                    # else emitting then-then-else reverses address order and the address-based
                    # gate mis-routes. Lexical order == address order is the invariant the gate
                    # relies on. (then_used/else_used can be empty when an arm is a pure
                    # terminal.) Exclude inlined RETURN blocks: a shared tail return (the
                    # early-return idiom inlines the SAME return at multiple jump sites,
                    # contract() bypasses it) is not part of an arm's exclusive address span —
                    # it floats, so it must not gate disjointness.
                    then_excl = [b for b in then_used if not _is_return_block(b, ctx)]
                    else_excl = [b for b in else_used if not _is_return_block(b, ctx)]
                    if then_excl and else_excl and max(then_excl) >= min(else_excl):
                        _audit_bail('arm_order')
                # LEXICAL-ADJACENCY of the merge: the gate lowers a construct's merge as the
                # address-next leader after its last block, so the merge MUST be that block.
                # Skipped when the merge is EXIT (the whole region ends after this if). Under the
                # Track B merge_nonadj flag (atom 8) this bail is suppressed: the if-node is built
                # even for a non-adjacent forward merge (the 77%-majority shared-merge fold). An
                # orphaned gap block left between the arm tail and the merge surfaces as a gate-
                # reject -> fall back; reduce()'s self-validation + _COMBOS acceptance backstop it.
                consumed = {n} | then_used | else_used
                if merge is not vm_cfg.EXIT and ctx.nxt[max(consumed)] != merge \
                        and not ctx.merge_nonadj:
                    _audit_bail('merge_not_adjacent')
                cond = vm_cfg._neg(rawcond)
                seq.append(('if', baddr if baddr is not None else n, stmts, cond, then_seq, else_seq))
                used |= consumed
                n = merge
            except _NotReducible as _ce:
                # Rung-6 fallback: keep this ONE branch as an honest `if (rawcond) goto L_t;` and
                # continue linearly at `fall`. SOUNDNESS GUARD: only valid when goto_block is
                # reachable FROM fall — then the linear continuation will actually walk + emit it
                # (a forward join). If it's reachable ONLY via this goto (a shared tail the fall
                # path returns/exits before hitting), the continuation would ORPHAN it (its
                # statements never emitted -> CFG divergence -> gate-reject). In that case
                # re-raise so the whole sub bails to flat (still sound). Restore ctx.visited
                # first (the failed arm probes marked blocks visited that must be re-walked).
                ctx.visited.clear()
                ctx.visited.update(saved_visited)
                ctx.dup_count.clear()
                ctx.dup_count.update(saved_dup)
                if goto_block is vm_cfg.EXIT:
                    raise
                ctx.visited.add(n)
                used.add(n)
                seq.append(('block', n, stmts))
                seq.append(('ifgoto', baddr if baddr is not None else n, rawcond, gtgt))
                n = fall
                continue
        else:
            raise _NotReducible('multiway')          # switch / >2-way / cond-less 2-way
    return seq, used


def _strip_trailing_continue(seq):
    """A loop body's natural tail loop-back is implicit (the closing `}` re-enters the
    header), so a trailing top-level `continue;` is redundant — drop it for readability.
    CFG-neutral: continue -> header is the same edge the fall-off back-edge supplies."""
    if seq and seq[-1][0] == 'term' and seq[-1][1] == 'continue':
        return seq[:-1]
    return seq


def _structure_loop(h, ctx, outer_loop):
    """Reduce the natural loop headed at `h` to one ('loop', …) node. Returns
    (node, consumed_leaders, after) where `after` is the block to continue the surrounding
    walk from (the loop's single exit, or EXIT if it only leaves via return)."""
    loop = ctx.loops[h]
    # RUNG 4d: nested loops compose for free — the body walk recurses into _structure_loop for
    # an inner header, collapsing it to one node the outer walk consumes like a block. A
    # multi-latch inner loop is NO LONGER bailed here (the old `nested_loop` guard): the
    # multi-latch handler below folds it to while(1)-with-continues and reduce() self-validates,
    # so nested multi-latch loops compose like any other (e.g. vm_bootstrap's per-turn/per-fief).
    # Exit target(s): a single clean exit, else not ours yet. An exit to a bare-RETURN block is
    # an EARLY RETURN, not a competing loop exit — the body walk's _terminal already inlines it
    # as `if (C) return X;` (CFG-neutral; contract() bypasses bare returns). So when a real
    # (non-return) exit exists, drop the return-block exits from the count: a loop with one
    # normal exit + N early returns collapses to the single-exit (pre/post-test) case and folds,
    # the returns falling out inside the body (e.g. $91A1: while-search with `return idx`). Only
    # when EVERY exit is a return block do we keep them (prior behaviour: one becomes the break
    # exit) — excluding them there would force e=None/while(1) and mis-anchor. (Tried widening
    # this to ALL return-EXITING blocks incl. multi-stmt `…; return` — net regression + a gate-
    # reject; multi-stmt returns aren't reliably clean early-return paths, so kept narrow.)
    all_exits = {s for nn in loop for s in ctx.full_cfg[nn]
                 if s is not vm_cfg.EXIT and s not in loop}
    # MULTI-LEVEL break: an exit that escapes the PARENT loop too is unreachable by a single
    # C `break` -> keep that edge as an honest `goto L_t` in the body (recorded in far_by_loop,
    # consumed by _terminal) and DROP it from this loop's exit set, so the remaining single-level
    # exit makes the loop foldable. Top-level loops (no parent) have nothing "far". This is what
    # lets a `goto DISPLAY` abort gate firing from N nested loops fold: each inner loop keeps the
    # goto; only the outermost loop that still CONTAINS the target breaks to it.
    if outer_loop is not None:
        parent_nodes = ctx.loops.get(outer_loop[0], frozenset())
        far = frozenset(s for s in all_exits if s not in parent_nodes)
    else:
        far = frozenset()
    ctx.far_by_loop[h] = far
    all_exits -= far
    shims = set()        # convergent-exit shim blocks pulled into the body (rung-6)
    # CONVERGENT-FIRST: if every loop exit funnels to ONE block (even a shared RETURN), that block is
    # the single real exit and the others are in-body break SHIMS — checked BEFORE the nonreturn
    # filter, which would otherwise pick a shim (a non-return block that itself flows to the shared
    # return) as a PHANTOM exit and mis-fold. This is the `$9C22`/`rot_incr_break` shape: the test
    # exits to a shared return AND a full guard-pass exits to `body_work` which flows to that same
    # return — the return is the exit, `body_work` is a shim pulled into the body (then it breaks).
    conv = _convergent_exit(all_exits, loop, ctx) if len(all_exits) > 1 else None
    if conv is not None:
        shims = all_exits - {conv}
        exit_targets = {conv}
    else:
        nonreturn = {s for s in all_exits if not _is_return_block(s, ctx)}
        if nonreturn:
            exit_targets = nonreturn
        elif all_exits:
            # Every exit is a return block. The loop's NATURAL exit is the header's own non-body
            # successor (the pre-test fall-out, e.g. $91A1's `return 255` tail); any OTHER return
            # exit is a body early return the walk inlines. Use the header-exit as primary when it's
            # unique; otherwise (post-test latch exit, or ambiguous) keep all so the count bails.
            header_exit = all_exits & ctx.full_cfg[h]
            exit_targets = header_exit if len(header_exit) == 1 else all_exits
        else:
            exit_targets = all_exits
        if len(exit_targets) > 1:
            # Still multi-exit after filtering: the targets may funnel to one block (the others are
            # loop-private exit shims). Only genuinely-divergent exits bail.
            c = _convergent_exit(exit_targets, loop, ctx)
            if c is None:
                # Genuinely-divergent multi-exit loop: no single construct owns it. Keep the LOOP as
                # a flat honest-goto span (rung-6 region-local fallback) and structure around it.
                return _flat_span(loop, ctx, ctx.nxt[max(loop)])
            shims = exit_targets - {c}      # walked in the body; extend the loop's emitted region
            exit_targets = {c}
    e = next(iter(exit_targets)) if exit_targets else None
    # The shims are walked inside the body, so the loop's emitted address span runs up to the
    # highest shim, not just max(loop) — the adjacency / consumed accounting below uses this.
    region = loop | shims
    # GATE ANCHORING: if the exit block is itself a do-while LATCH (a 2-way post-test rendered as
    # the `} while (C);` scaffold line), the gate's first_real_block skips PAST it to the block
    # after the do-while and mis-reads THIS loop's exit. The reducer's output is correct C, but
    # the verifier can't anchor an exit on a scaffold line -> bail (sound). Hits an inner while
    # that is the last statement of a do-while body (e.g. $DB97: while(...) ending a do-while).
    # ONLY when the parent is a SINGLE-latch do-while (so e really renders as `} while(C);`): a
    # MULTI-latch parent renders as while(1), where e (a back-edge block) is a normal `if(cond)
    # continue; …` block the gate anchors on fine — bailing there blocks valid nested folds.
    if outer_loop is not None and e is not None and len(ctx.full_cfg[e]) == 2 \
            and len(ctx.latches.get(outer_loop[0], ())) == 1 \
            and e in ctx.latches.get(outer_loop[0], ()) and not ctx.info[e][0]:
        _audit_bail('exit_is_dowhile_latch')
    # LEXICAL ADJACENCY of the exit: the gate reads a loop's exit as the first REAL block AFTER
    # the closing brace by address, so the exit must be the leader right after the loop's last
    # block. (ROUTING-BLOCK ABSORPTION, atom #3: a pure-routing exit — a 1-succ `goto L_t` block
    # with no statement, the compiler's fall-through-forwarding stub between chained loops — is
    # NO LONGER bailed here. It emits as an empty block, so the gate's first_real_block scans
    # PAST it to the real target `t`, and contract() drops the stub on BOTH sides (raw + struct),
    # so they match. This folds loops chained via `goto NEXT` entries, e.g. init_new_game_state's
    # for-loop sequence. reduce()'s self-validation is the backstop for any mis-route.)
    if e is not None and ctx.nxt[max(region)] != e:
        # The loop's exit isn't the address-next block. Keep the LOOP as a flat honest-goto span
        # and structure around it (rung-6 region-local fallback); continue at the address-next
        # block so the walk picks up the between-blocks and the real exit.
        return _flat_span(region, ctx, ctx.nxt[max(region)])
    lp = (h, e)
    # Body-local post-dominator: an edge that LEAVES the body (to the header = continue, to
    # the exit / outside = break, to EXIT = return) is TERMINAL — it must not pull the merge
    # toward itself, so it is DROPPED from the merge graph. A block whose every edge was
    # dropped (a latch, an unconditional break) becomes a sink -> {EXIT} so post_dominators
    # stays well-defined (an edge-LESS node would wrongly post-dominate to ALL nodes). The
    # merge of an in-body if is then where its NON-terminal paths reconverge, e.g. the latch.
    merge_src = {}
    for nn in loop:
        keep = frozenset(s for s in ctx.full_cfg[nn]
                         if s is not vm_cfg.EXIT and s in loop and s != h)
        merge_src[nn] = keep if keep else frozenset({vm_cfg.EXIT})
    body_pdom = vm_cfg.post_dominators(merge_src)

    hsucc = ctx.full_cfg[h]
    h_stmts, h_rawcond, h_baddr, h_gtgt, _h_sw = ctx.info[h]

    # A clean post-test latch: a SINGLE 2-way latch whose two edges are (back-to-header, exit).
    # Such a loop is a do-while — even when the header ALSO 2-way-tests the exit (so it would
    # ALSO match the pre-test shape). Prefer do-while there: the pre-test rendering would put the
    # latch's body block as a while-body tail that EXITS via fall (a `break`), and a body-tail
    # break is unrepresentable for the gate (its per-block lowering keeps one terminator/block,
    # so the `continue` edge is lost). V1 renders these as do-while too. (Computed before the
    # pre-test so it can gate it; a 1-way for-loop update latch is NOT 2-way -> doesn't divert.)
    latches2 = [u for u in ctx.latches[h]
                if len(ctx.full_cfg[u]) == 2 and h in ctx.full_cfg[u]
                and e is not None and e in ctx.full_cfg[u]]
    clean_dowhile = e is not None and len(ctx.latches[h]) == 1 and bool(latches2)

    # ---- pre-test while: the header itself is the 2-way exit test ----------------------
    # The in-loop successor must be a DISTINCT body block, not the header itself: a self-loop
    # (header == latch, the test at the bottom of its single block) is a do-while, not a
    # pre-test while — treating it as pre-test sets body_entry = h, which is already visited
    # and raises a spurious cross_edge. So require a non-self in-loop successor here and let a
    # self-loop fall through to the do-while detection below (which carries the body in latch_stmts).
    if (not clean_dowhile and e is not None and len(hsucc) == 2 and h_rawcond is not None
            and e in hsucc and any(s in loop and s != h for s in hsucc)):
        body_entry = next(s for s in hsucc if s in loop and s != h)
        cond = h_rawcond if ctx.block_of(h_gtgt) in loop else vm_cfg._neg(h_rawcond)
        ctx.visited.add(h)
        body, _u = _structure(body_entry, _STOP, body_pdom, ctx, lp)
        body = _strip_trailing_continue(body)
        # Only the test EXPRESSION lifts to `while (cond)`, so a header that carries statements
        # before its test cannot be a pre-test while: those statements run on EVERY arrival at the
        # test, the loop's entry included. Lifting them to the body bottom (the old rule, which
        # read them as a rotated for-loop's update) drops the entry run, and the C reads a variable
        # before setting it — the address-keyed gate cannot see it, since the CFG is unchanged
        # (2026-10-28: 42 NA1 loops whose header was a phi's merge store, avg_fief_tax_rate $952F).
        # A for-loop's update lives in the LATCH, not the header. coalesce_store_phis now empties
        # the phi headers before structuring; any header that still has statements bails here to
        # the do-while / while(1) / honest-goto forms, which keep the statements where they run.
        if h_stmts:
            raise _NotReducible('pretest_header_stmts')
        node = ('loop', 'while', h, cond, body, None, [])
        ctx.visited |= set(loop)
        return node, set(loop) | shims, e

    # ---- post-test do-while: a single latch's 2-way test gates the back edge -----------
    if clean_dowhile:
        u = latches2[0]
        u_stmts, u_rawcond, _u_baddr, u_gtgt, _u_sw = ctx.info[u]
        cond = u_rawcond if ctx.block_of(u_gtgt) == h else vm_cfg._neg(u_rawcond)
        body, _u = _structure(h, u, body_pdom, ctx, lp)
        ctx.visited.add(u)
        ctx.visited |= set(loop)
        node = ('loop', 'dowhile', h, cond, body, u, u_stmts)
        return node, set(loop) | shims, e

    # ---- self-loop with no clean exit (header is its own only body, succ includes h) ----
    # An infinite self-loop can't be walked as a body (the body IS the header, which is
    # already visited -> a spurious cross_edge that flattened the whole sub). Fold to
    # while(1){stmts} when the block carries statements (the gate's tail-falls-off rule
    # rebuilds the self back-edge from the body tail); when it's a bare `goto self` with no
    # statement, while(1){} would DROP the back-edge (empty body = no tail) so keep it as an
    # honest self-goto. Either way the REST of the sub stays structured — the rung-6
    # local-honest-goto idea applied to the one shape that needs it most. (A self-loop WITH
    # an exit edge is a do-while, already handled above via latch_stmts.)
    if loop == frozenset({h}):
        ctx.visited.add(h)
        if h_stmts:
            node = ('loop', 'while1', h, None, [('block', h, h_stmts)], None, [])
        else:
            node = ('selfgoto', h)
        return node, {h}, (e if e is not None else vm_cfg.EXIT)

    # ---- multi-latch loop -> while(1) with `continue` guards (else flat honest-goto span) ----
    # A loop with >1 latch is a while(1) whose extra back-edges are `continue` statements (the
    # inverse of the compiler lowering each `continue` to a jump-to-header). ATTEMPT that fold:
    # walk the body (edges to the header render as `continue`, to the exit as `break`). The one
    # UNSOUND shape is a body-TAIL `if(C) continue;` whose fall-through IS the loop-back — the
    # gate then reads the fall as the back-edge and loses the exit; the both-terminal walk now
    # collapses such a tail to a single `if(!C) break;` guard, and reduce()'s self-validation is
    # the backstop. If the body walk can't own a shape, fall back to the flat honest-goto span
    # (the surrounding loops/ifs still fold), restoring `visited` so the bailed blocks re-walk.
    if len(ctx.latches[h]) > 1:
        saved = set(ctx.visited)
        try:
            body, _u = _structure(h, _STOP, body_pdom, ctx, lp)
            body = _strip_trailing_continue(body)
            ctx.visited |= set(loop)
            return ('loop', 'while1', h, None, body, None, []), set(loop) | shims, (e if e is not None else vm_cfg.EXIT)
        except _NotReducible:
            ctx.visited.clear()
            ctx.visited.update(saved)
            return _flat_span(loop, ctx, e if e is not None else vm_cfg.EXIT)

    # ---- endless: while(1) { body }  (exits via break / return inside) -----------------
    # The header h IS the first body block of a while(1) (no governing pre-test), so the body
    # walk must START at h and EMIT it. Do NOT pre-mark h visited: _structure(h) checks
    # `n in visited` on its first step and would raise a spurious cross_edge (the multi-latch
    # bug — e.g. $88AC's {8908,8911}). Edges back to h are already resolved as `continue` by
    # _terminal (keyed on b == h, not on visited), so no pre-add is needed for correctness;
    # the whole loop is marked visited AFTER the walk.
    body, _u = _structure(h, _STOP, body_pdom, ctx, lp)
    body = _strip_trailing_continue(body)
    ctx.visited |= set(loop)
    return ('loop', 'while1', h, None, body, None, []), set(loop) | shims, (e if e is not None else vm_cfg.EXIT)


def _structure_switch(S, ctx, loop):
    """Rung 5: fold the foldable switch dispatched at block `S` to ONE composable node so the
    walk consumes it like a block (and any enclosing loop/if folds around it). Block-level
    port of vm_decompile.structure_switches: keep the `switch (E) {` opener, DROP the
    `case N: goto L_T;` dispatch lines + the closer, then inline each case-target block's raw
    lines (verbatim — internal gotos/returns stay honest, exactly as V1) in ADDRESS order with
    `case N:` / `default:` labels injected, +1 indent, closing `}` after the last enclosed
    block. Returns ('switchspan', rel_lines), consumed_leaders, after. `rel_lines` is a list of
    (addr, rel_indent, text); _emit adds the base indent so it nests under loops/ifs.
    Faithful (no break-conversion, no intra-case structuring yet — those are follow-ups); its
    CFG matches the raw witness the same way V1's inline does, so the gate accepts it."""
    d = ctx.switches[S]
    body_end = d['body_end']
    M = d.get('merge')
    # atom 4: an EMPTY default whose target IS the shared merge M. Then M is the switch EXIT
    # (pulled out past the `}`; the walk resumes there), NOT a case body; each case renders
    # `break;` to it. dim gates the whole break-conversion path (else: legacy goto-inline).
    dim = bool(d.get('default_is_merge')) and d.get('break_target') is not None
    after = M if dim else (ctx.block_of(body_end) if body_end is not None else vm_cfg.EXIT)
    goto_m = f"goto L_{M:04X};" if dim else None
    # target block -> the case-label lines to emit before it, in key-declaration order.
    by_target = {}
    for key, tgt in d['key_targets']:
        if dim and key is None and tgt == M:
            continue                           # omit the empty default's label (implicit -> M)
        by_target.setdefault(tgt, []).append('default:' if key is None else f'case {key}:')

    rel = []                                   # (addr, rel_indent, text)
    # --- S's block: leading selector stmts + the `switch (E) {` opener; skip dispatch+closer.
    opener_seen = False
    for addr, _i, text in ctx.buckets[S]:
        t = text.strip()
        if vm_cfg._RE_LABEL.match(t):
            continue
        if opener_seen:
            continue                           # dispatch `case: goto` lines + the `}` closer
        rel.append((addr, 0, t))
        if vm_cfg._RE_SWITCH_OPEN.match(t):
            opener_seen = True
    # --- enclosed case bodies: every leader strictly between S and body_end, address order.
    # In the dim path the merge M is EXCLUDED (it's pulled out past the `}`).
    enclosed = [L for L in d['enclosed'] if L != S
                and (body_end is None or L < body_end)
                and not (dim and L == M)]
    consumed = {S} | set(enclosed)
    labeled = set()
    for L in sorted(enclosed):
        if L in by_target:
            for lab in by_target[L]:
                rel.append((L, 1, lab))           # case label aligns one in from `switch`
            labeled.add(L)
        body = list(ctx.buckets[L])
        emitted_break = False
        for j, (addr, _i, text) in enumerate(body):
            t = text.strip()
            if dim and t == goto_m and j == len(body) - 1:
                rel.append((addr, 2, 'break;'))   # case exits to the merge -> break
                emitted_break = True
            else:
                rel.append((addr, 2, t))          # case body one deeper than its label
        # a case body that FALLS to the merge with no explicit terminator gets a break too
        # (e.g. $A30D case 0: per_turn_age() then falls into A381). A body that falls to the
        # NEXT case (real C fall-through) or ends in return/other-goto is left as-is.
        if dim and not emitted_break and ctx.full_cfg.get(L) == frozenset({M}):
            lastt = body[-1][2].strip() if body else ''
            cl, _t = vm_cfg._classify_stmt(lastt) if body else ('plain', None)
            if cl == 'plain':
                rel.append((body[-1][0] if body else L, 2, 'break;'))
    # --- empty trailing cases: a key pointing at the merge (== body_end) has no enclosed
    #     block; emit it as an empty case that falls off the `}` (== break / fall-to-merge).
    for tgt in sorted(t for t in by_target if t not in labeled):
        if tgt == after and after is not vm_cfg.EXIT:
            for lab in by_target[tgt]:
                rel.append((tgt, 1, lab))
            labeled.add(tgt)
    rel.append((0, 0, "}"))
    # --- prune case-target leader labels whose only refs were the dropped dispatch lines.
    referenced = set()
    for _a, _ri, txt in rel:
        for m in re.finditer(r'goto L_([0-9A-Fa-f]{4})', txt):
            referenced.add(int(m.group(1), 16))
    pruned = []
    for a, ri, txt in rel:
        lm = re.match(r'L_([0-9A-Fa-f]{4}):$', txt.strip())
        if lm and a in labeled and int(lm.group(1), 16) not in referenced:
            continue
        pruned.append((a, ri, txt))
    return ('switchspan', pruned), consumed, after


def _flat_span(blocks, ctx, after):
    """RUNG 6 — region-local honest-goto fallback (the audit's `_flat_span`). A region the
    grammar can't fold to a SOUND construct (here: a multi-latch loop whose `while(1)`
    exit-via-fall is ambiguous to the gate — e.g. $88AC's {8908,8911}) is kept as its RAW
    lines (gotos/labels verbatim, address order) and the surrounding walk structures
    everything AROUND it — exactly as V1 leaves such loops as honest gotos. CFG-faithful by
    construction (the raw lines ARE the witness), so it never gate-rejects; only this one
    region stays flat. Returns ('flatspan', rel_lines), consumed, after — rel_lines =
    (addr, 0, text) the emitter places at the base indent (labels kept for the goto targets)."""
    # Strip the raw `L_xxxx:` labels (like the structured emit does); _insert_labels re-adds
    # exactly the ones an emitted goto targets, so a label isn't duplicated when the span's
    # entry/targets are also goto destinations (e.g. $88AC's L_8908).
    rel = [(addr, 0, text.strip()) for L in sorted(blocks) for addr, _i, text in ctx.buckets[L]
           if not vm_cfg._RE_LABEL.match(text.strip())]
    ctx.visited |= set(blocks)
    return ('flatspan', rel), set(blocks), after


def _emit(seq, indent, out):
    for item in seq:
        kind = item[0]
        if kind == 'block':
            _, _L, stmts = item
            for addr, text in stmts:
                out.append((addr, indent, text))
        elif kind == 'term':
            out.append((item[2], indent, f"{item[1]};"))
        elif kind == 'guard':
            _, baddr, cond, gkind = item
            out.append((baddr, indent, f"if ({cond}) {gkind};"))
        elif kind == 'selfgoto':
            # An irreducible bare self-loop (`L_h: goto L_h;`) — kept as an honest goto so
            # the rest of the sub still structures. The label is supplied by _insert_labels.
            h = item[1]
            out.append((h, indent, f"goto L_{h:04X};"))
        elif kind == 'ifgoto':
            # Rung-6 honest conditional goto: a 2-way branch the structurer couldn't cleanly
            # own, kept verbatim as `if (rawcond) goto L_t;` (target label via _insert_labels).
            _, baddr, rawcond, tgt = item
            out.append((baddr, indent, f"if ({rawcond}) goto L_{tgt:04X};"))
        elif kind == 'goto':
            # Rung-6 unconditional cross-edge cut: block `src` falls into an already-emitted
            # shared continuation `tgt`; kept as an honest `goto L_tgt;` (tagged with src's
            # addr so the gate attributes the edge to that block; label via _insert_labels).
            _, src, tgt = item
            out.append((src, indent, f"goto L_{tgt:04X};"))
        elif kind == 'dup':
            # atom 5: a DUPLICATED terminal sink, inlined into host block `src`. Re-tag every copied
            # line to src's leader addr so block_of maps the copy to src (NOT the shared sink) — the
            # host arm then ends in its OWN copy of the tail (-> EXIT), recovering the un-cross-jumped
            # form. (src has its own stmts emitted just before this node.)
            _, src, _sink, stmts = item
            for _a, text in stmts:
                out.append((src, indent, text))
        elif kind == 'ifdup':
            # UN-SHARING: a guard duplicating a bounded shared block into its arm, `if(cond){ copy;
            # term; }`. The copy's lines go at a SYNTHETIC addr (so N copies don't collide) and end in
            # `term` (continue / break) so the synth block routes like the shared original.
            _, baddr, cond, _orig, s_addr, stmts, term = item
            out.append((baddr, indent, f"if ({cond}) {{"))
            for _a, text in stmts:
                out.append((s_addr, indent + 1, text))
            if term:
                out.append((s_addr, indent + 1, f"{term};"))
            out.append((0, indent, "}"))
        elif kind in ('switchspan', 'flatspan'):
            for addr, rel_indent, text in item[1]:
                out.append((addr, indent + rel_indent, text))
        elif kind == 'if':
            _, baddr, stmts, cond, then_seq, else_seq = item
            for addr, text in stmts:
                out.append((addr, indent, text))
            out.append((baddr, indent, f"if ({cond}) {{"))
            _emit(then_seq, indent + 1, out)
            if else_seq:
                out.append((0, indent, "} else {"))
                _emit(else_seq, indent + 1, out)
            out.append((0, indent, "}"))
        else:  # 'loop'
            _, form, haddr, cond, body, latch_addr, latch_stmts = item
            if form == 'while':
                out.append((haddr, indent, f"while ({cond}) {{"))
            elif form == 'while1':
                out.append((haddr, indent, "while (1) {"))
            else:  # dowhile
                out.append((haddr, indent, "do {"))
            _emit(body, indent + 1, out)
            if form == 'dowhile':
                for addr, text in latch_stmts:
                    out.append((addr, indent + 1, text))
                out.append((latch_addr, indent, f"}} while ({cond});"))
            else:
                out.append((0, indent, "}"))


# atom 8: the structuring trial grid — (sink_merge, merge_nonadj). Track A's forward-sink atom
# crossed with Track B's non-adjacent-merge fold. reduce() runs the whole-sub trial AND each
# region-fallback region over this grid and keeps the fewest-goto VALIDATING result per pass
# (goto-count-aware acceptance; the gate proves only equivalence, not minimality). Order is
# deterministic; on a goto-count tie the earliest combo wins (plain off < +A < +B < +A+B).
_COMBOS = ((False, False), (True, False), (False, True), (True, True))
# The region-fallback uses a NARROWER grid (no merge_nonadj): the per-region acceptance is GREEDY
# (commit region i's leanest validating structuring, then region j), and a merge_nonadj fold that
# is leaner for region i can BLOCK a later region from structuring — netting MORE gotos than
# leaving i flat ($8694: full grid 11 vs sink-only 9). merge_nonadj pays off in the WHOLE-SUB
# trial (one global structuring, no greedy seam); in the fallback it's a regression risk for no
# measured gain, so the fallback stays sink-only. (Revisit if a global region assignment replaces
# the greedy one.)
_REGION_COMBOS = ((False, False), (True, False))


def reduce(lines, instructions):
    """Region reducer (CFG epic V2). Rungs 0-1 = block partition + flat emit; rungs 2+4 add
    sequence / if-then / if-then-else / while / do-while / while(1) over the whole sub.
    Falls back to the rung-1 flat emit whenever the structure can't be cleanly recovered;
    the CFG-equivalence gate in vm_decompile.decompile is the final backstop."""
    global LAST_BAIL
    LAST_BAIL = None
    cfg, leaders = vm_cfg.bytecode_cfg(instructions)
    if not leaders:
        return list(lines)
    buckets = _partition(lines, leaders)
    block_of = vm_cfg._block_of_fn(leaders)
    info = {L: _parse_block(buckets[L]) for L in leaders}
    # Rung 5: foldable switches (single-entry dispatch) become composable region nodes.
    # An UN-foldable switch is handled LOCALLY inside _structure (flat-span its dispatch,
    # structure the case bodies around it — rung 6), not by bailing the whole sub.
    switches = {d['S']: d for d in vm_cfg.switch_dispatches(instructions)}
    nxt = {leaders[i]: (leaders[i + 1] if i + 1 < len(leaders) else vm_cfg.EXIT)
           for i in range(len(leaders))}
    edges, _dom = vm_cfg.back_edges(cfg, leaders[0])
    from collections import defaultdict
    loops, latches = {}, defaultdict(set)
    for u, hh in edges:
        loops.setdefault(hh, set()).update(vm_cfg.natural_loop(u, hh, cfg))
        latches[hh].add(u)
    loops = {hh: frozenset(s) for hh, s in loops.items()}
    pdom = vm_cfg.post_dominators(cfg)

    def _fresh_ctx(sink_merge=False, merge_nonadj=False, dup=False):
        c = _Ctx(cfg, info, block_of, nxt, loops, latches, buckets, switches)
        c.sink_merge = sink_merge
        c.merge_nonadj = merge_nonadj
        c.dup_terminals = dup
        return c

    def _dup_guard_ok(ctx):
        """Content backstop for un-sharing: every DUPLICATED shared block T must have exactly as many
        copies as it has WITNESS in-edges — so no path silently dropped T's content (the routing gate
        is BLIND to this: contract() bypasses T + its copies, so a missing copy reads the same). copies
        = dup_count[T] + (1 if T's ORIGINAL was emitted, i.e. T ∈ visited, else 0). The cross-edge dup
        keeps the original (T visited) + 1 dup; a guard un-shares ALL preds (T never walked → 0
        original). Either way copies must == |preds(T)|."""
        return all(ctx.dup_count[T] + (1 if T in ctx.visited else 0) == len(ctx.preds[T])
                   for T in ctx.dup_count)

    def _structure_region(entry, stop, sink_merge=False, merge_nonadj=False, dup=False):
        """Structure the single-entry/single-exit region [entry, stop) into a seq (AST items),
        or None on a structural bail. Fresh visited per call (regions are disjoint). When `dup`
        duplicated a sink, returns None unless the copies==in-edges content guard holds."""
        ctx = _fresh_ctx(sink_merge, merge_nonadj, dup)
        try:
            seq, _u = _structure(entry, stop, pdom, ctx, None)
        except _NotReducible:
            return None
        if dup and not _dup_guard_ok(ctx):
            return None                 # an incomplete duplication — unsound, reject
        return seq

    def _seq_gotos(seq):
        """goto count of an emitted (validated) line list — the goto-count-aware tie-breaker."""
        return sum(1 for _a, _i, t in seq if _RE_GOTO_TGT.search(t))

    def _validates(seq):
        out = []
        _emit(seq, 1, out)
        out = _insert_labels(out, block_of)
        out = _label_reordered_falls(out, block_of)   # approach B: anchor backward fall targets
        if not vm_cfg.structured_equivalent(lines, out, leaders)[0]:
            # REDUCER HONESTY: a reordered fall-through seam reads wrong (the emit-order gate caught
            # it). Repair the seam with an explicit goto and keep the rest of the fold, rather than
            # discarding the whole structuring. Re-label (the new goto needs its target label) and
            # re-validate; if the repair doesn't fully resolve it, fall through to the old fallback.
            rep = _honest_repair(out, cfg, leaders, block_of)
            if rep is None:
                return None
            rep = _label_reordered_falls(_insert_labels(rep, block_of), block_of)
            if not vm_cfg.structured_equivalent(lines, rep, leaders)[0]:
                return None
            out = rep
        # CFG-neutral peephole (drop redundant goto-to-fall-through + dead labels). Each goto-drop
        # is validated on its own (emit-order adjacency can disagree with the gate at a reordered
        # flat-span boundary — a per-clip check keeps every sound drop and reverts only the unsound
        # ones). The final wholesale re-check is the backstop (dead-label removal is gate-neutral).
        # Re-anchor backward fall labels after (peephole's dead-label sweep may drop a goto-less one).
        peeled = _peephole(out, validate=lambda s: vm_cfg.structured_equivalent(lines, s, leaders)[0])
        peeled = _label_reordered_falls(peeled, block_of)
        return peeled if vm_cfg.structured_equivalent(lines, peeled, leaders)[0] else out

    # GLOBAL goto-count acceptance (atom 8). The whole-sub structuring and the region-level
    # fallback are BOTH computed and the fewest-goto VALIDATING one wins. A whole-sub fold that
    # merely validates must NOT short-circuit a leaner region-fallback — the $8694 trap: with
    # merge_nonadj on, the whole sub validates at 11 gotos, but the region fallback yields 9;
    # local "first whole-sub that validates wins" returned 11. Each pass runs over the _COMBOS
    # grid (sink_merge × merge_nonadj) keeping its own fewest-goto validating result.

    # 1) WHOLE-SUB structuring, self-validated against the CFG-equivalence gate (the design's
    #    verifier). Composed local fallbacks (honest-goto cuts / flat spans / convergent exits)
    #    can emit a CFG-divergent LAYOUT no structural guard caught; rather than enumerate every
    #    hazard, prove equivalence or fall through. Keeps "0 gate-rejects downstream" automatic.
    #    Track A (sink_merge) helps the forward-sink subs ($9F04, …); Track B (merge_nonadj) the
    #    non-adjacent forward shared-merge majority; a few subs fold leaner under neither ($A853).
    #    First-valid-wins would lock in a worse fold (the gate proves equivalence, not goto-count).
    #    atom 5 (dup): each combo is also trialled with terminal-DUPLICATION on; the fewest-goto
    #    validating fold wins, so the dup only sticks where it actually removes cross-jump gotos.
    whole_best = None
    for _sm, _mn in _COMBOS:
      for _dup in (False, True):
        whole = _structure_region(leaders[0], vm_cfg.EXIT,
                                  sink_merge=_sm, merge_nonadj=_mn, dup=_dup)
        if whole is None:
            continue
        out = _validates(whole)
        if out is not None and (whole_best is None or _seq_gotos(out) < _seq_gotos(whole_best)):
            whole_best = out
    # A clean 0-goto whole-sub fold is optimal — no region-fallback can beat it, so skip the
    # (now always-run) region decomposition for the common case (keeps the cost increase to subs
    # that still carry residual gotos).
    if whole_best is not None and _seq_gotos(whole_best) == 0:
        return whole_best

    # 2) REGION-LEVEL PARTIAL FALLBACK. Decompose the sub at its post-dominator SPINE (the
    #    immediate-post-dom chain entry -> … -> EXIT — the blocks ALL paths pass through) into a
    #    sequence of single-entry/single-exit regions. Structure each independently and keep it
    #    only if (that region structured + every OTHER region flat) self-validates; else flat-span
    #    just that region. Regions are disjoint and joined linearly at the articulation points, so
    #    a divergence in one improper region no longer flattens the WHOLE sub — the reducible
    #    regions around it keep their structure. Every kept piece is CFG-proven.
    region_best, region_has_structure = None, False
    spine, p, seen = [leaders[0]], leaders[0], {leaders[0]}
    while p is not vm_cfg.EXIT:
        p = vm_cfg._imm_post_dom(pdom, p)
        if p in seen:                      # malformed / cyclic spine — abandon recovery
            spine = None
            break
        seen.add(p)
        spine.append(p)
    if spine and len(spine) > 2:           # >1 region, else recovery == whole-sub (already failed)
        regions = list(zip(spine, spine[1:]))
        # Each region (Pi, Pj) owns the blocks reachable from Pi walled off by EVERY OTHER spine
        # point — not just Pj. Walling only Pj is unsound when the spine cuts through a loop: a
        # back-edge from a LATER spine point (a loop latch) re-enters an EARLIER region's body, so
        # that region's flat-span re-emits the whole loop body as unreachable dead code (CFG-
        # equivalent, so the gate misses it, but it doubles the gotos). Spine points are
        # articulation points, so nothing legitimately between Pi and Pj is itself a spine point —
        # walling them all off makes the regions a true disjoint partition.
        spine_walls = set(spine) - {vm_cfg.EXIT}
        rblocks = [{b for b in _reachable_avoiding(Pi, spine_walls - {Pi}, _fresh_ctx())
                    if b is not vm_cfg.EXIT}
                   for Pi, Pj in regions]
        flat = [[_flat_span(bl, _fresh_ctx(), None)[0]] for bl in rblocks]   # one flatspan node
        chosen = [seq[:] for seq in flat]                                    # default: all flat

        def _gotos_of(seq):
            o = []
            _emit(seq, 1, o)
            return sum(1 for _a, _i, t in o if _RE_GOTO_TGT.search(t))

        flat_g = [_gotos_of(seq) for seq in flat]
        for i, (Pi, Pj) in enumerate(regions):
            # Try the whole _COMBOS grid (sink_merge × merge_nonadj); take the leaner valid one.
            # (Terminal-dup is trialled in the WHOLE-SUB pass only — the cross-jump shapes it owns
            # fold there; adding it to the greedy per-region grid gave 0 corpus gain for extra cost.)
            cands = [r for r in (_structure_region(Pi, Pj, sink_merge=sm, merge_nonadj=mn)
                                 for (sm, mn) in _REGION_COMBOS) if r is not None]
            s = min(cands, key=_gotos_of) if cands else None
            # Keep the region structured only if it BOTH validates AND emits no more gotos than
            # the flat span — an honest-goto cut can turn a raw fall-through into an explicit
            # goto, so an over-aggressive structuring may carry MORE gotos than the leaner flat
            # form; prefer flat there (fewer gotos, and the gate-faithful raw layout).
            if s is None or _gotos_of(s) > flat_g[i]:
                continue
            trial = chosen[:i] + [s] + chosen[i + 1:]
            if _validates([it for sub in trial for it in sub]) is not None:
                chosen[i] = s
        out = _validates([it for sub in chosen for it in sub])
        if out is not None:
            region_best = out
            region_has_structure = any(chosen[i] is not flat[i] for i in range(len(regions)))

    # 3) GLOBAL pick — fewest-goto of whole-sub vs region-fallback. On a tie the whole-sub form
    #    wins: it is the cleaner single construct (no artificial region seams). LAST_BAIL records
    #    the disposition for the bail audit: None = a structured fold landed; 'self_gate' = nothing
    #    structured validated, so the result is the honest flat layout.
    if whole_best is not None and (region_best is None
                                   or _seq_gotos(whole_best) <= _seq_gotos(region_best)):
        LAST_BAIL = None
        return whole_best
    if region_best is not None:
        LAST_BAIL = None if region_has_structure else 'self_gate'
        return region_best
    LAST_BAIL = 'self_gate'
    return _flat_emit(buckets, leaders)


_RE_GOTO_TGT = re.compile(r'goto L_([0-9A-Fa-f]{4})')


def _honest_repair(out, cfg, leaders, block_of):
    """REDUCER HONESTY (approach B, the reducer side). A candidate fold can place a PLAIN
    single-successor block so that it physically falls through to a NON-successor (the tree walk /
    region seam emitted a reordered block next, and the block carried no terminator). The old
    address-based gate laundered this — it read the fall as the ADDRESS-next leader, which happened
    to match the CFG — but the emitted C READS WRONG (control visibly falls into the wrong block,
    e.g. $ADF6's `*(fp-45)=1;` falling into the `ppu_blit` of $AE2A instead of $AE78). The
    emit-order gate now rejects it. Rather than discard the whole (otherwise-correct) fold, repair
    the seam: emit an explicit `goto L_<succ>` after the offending block so the C reads correctly
    and the surrounding structure is kept.

    Invoked ONLY after a fold fails `structured_equivalent`; the caller re-labels + re-validates, so
    an incomplete repair just falls through to the old fallback. Conservative by construction — it
    touches ONLY a plain block whose EMIT-order successor (lower_struct_cfg, the gate's own view)
    differs from its single CFG successor, so a clean fold (or a CORRECT reorder like atom-5's dup
    arm, whose emit successor DOES match) is never given a spurious goto. `<succ>` is the CFG
    successor chased through EMPTY (line-less, single-succ) blocks to the first one that carries an
    emitted line, so the goto's `L_xxxx:` label has somewhere to land."""
    EXIT = vm_cfg.EXIT
    cfg_str = vm_cfg.lower_struct_cfg(out, leaders)
    has_content, last_line = set(), {}
    for i, (a, ind, t) in enumerate(out):
        ts = t.strip()
        if vm_cfg._RE_LABEL.match(ts):
            continue
        if vm_cfg._is_scaffold(ts) and not vm_cfg._opens_block(ts):
            continue
        L = block_of(a)
        has_content.add(L)
        last_line[L] = (i, ind, a)              # block L's last emitted occupying line

    def chase(t):
        seen = set()
        while t is not EXIT and t not in has_content and t not in seen:
            seen.add(t)
            s = cfg.get(t)
            if s and len(s) == 1:
                (t,) = tuple(s)
            else:
                break
        return t

    inserts = {}
    for L in leaders:
        true_s, emit_s = cfg.get(L), cfg_str.get(L)
        if not true_s or len(true_s) != 1 or not emit_s or len(emit_s) != 1:
            continue
        (ts_,), (es_,) = tuple(true_s), tuple(emit_s)
        if ts_ is EXIT or ts_ == es_:           # correct fall (incl. a sound backward reorder)
            continue
        li = last_line.get(L)
        if li is None:
            continue
        i, ind, a = li
        if vm_cfg._classify_stmt(out[i][2].strip())[0] != 'plain':   # only a pure fall-through seam
            continue
        tgt = chase(ts_)
        if tgt is EXIT or tgt not in has_content:
            continue
        inserts[i] = (ind, a, tgt)
    if not inserts:
        return None
    res = []
    for i, line in enumerate(out):
        res.append(line)
        if i in inserts:
            ind, a, tgt = inserts[i]
            res.append((a, ind, f"goto L_{tgt:04X};"))    # tagged with L's addr -> gate keys L->tgt
    return res


def _label_reordered_falls(out, block_of):
    """approach B (reducer side): label a BACKWARD fall target. When the emit places a block X
    physically right after a fall-through block P but X has a LOWER address than P (an address-
    inverted arm — e.g. atom-5's dup puts $AD67's body after the $AD7B guard, and $AD67 < $AD7B),
    the emit-order gate resolves P's fall by X's LABEL (lex_fall keys on labels — flush-safe). The
    dup removed the `goto` that used to label X, so nothing references it; emit an explicit `L_X:`
    so the gate can anchor the reordered fall. INERT on address-ordered emit (X is always > P
    there, forward), so a normal fold gains no label. Idempotent (skips already-labelled blocks)."""
    existing = {int(t.strip()[2:6], 16) for _a, _i, t in out
                if vm_cfg._RE_LABEL.match(t.strip())}
    to_label, prev, prev_falls = set(), None, False
    for a, _ind, text in out:
        ts = text.strip()
        if vm_cfg._RE_LABEL.match(ts):
            prev, prev_falls = int(ts[2:6], 16), False
            continue
        if ts == '}' or ts == '} else {' or vm_cfg._RE_DOWHILE_CLOSE.match(ts):
            prev, prev_falls = None, False
            continue
        if vm_cfg._opens_block(ts):
            # A construct OPENER (`while/if/switch/do {`): its body entry is resolved by the
            # construct (header_edges / body_entry), NOT a lexical fall — so the opener must not
            # mark the body as a backward fall target (the spurious `L_xxxx:` after `while(p()){`
            # on a rotated loop, where the body addr < the bottom-test header addr).
            prev, prev_falls = block_of(a), False
            continue
        if (vm_cfg._is_scaffold(ts) and not vm_cfg._opens_block(ts)) \
                or vm_cfg._RE_CASE_LABEL.match(ts):
            continue
        X = block_of(a)
        if X != prev:
            if prev is not None and prev_falls and X < prev and X not in existing:
                to_label.add(X)
            prev = X
        prev_falls = vm_cfg._classify_stmt(ts)[0] in (
            'plain', 'ifgoto', 'ifreturn', 'ifbreak', 'ifcontinue')
    if not to_label:
        return out
    res, placed = [], set()
    for a, ind, text in out:
        ts = text.strip()
        if not (vm_cfg._RE_LABEL.match(ts)
                or (vm_cfg._is_scaffold(ts) and not vm_cfg._opens_block(ts))
                or vm_cfg._RE_CASE_LABEL.match(ts)):
            X = block_of(a)
            if X in to_label and X not in placed:
                res.append((X, 0, f"L_{X:04X}:"))
                placed.add(X)
        res.append((a, ind, text))
    return res


def _insert_labels(out, block_of):
    """Re-insert `L_xxxx:` labels for any honest goto the structured emit produced (rung-6
    self-gotos / edge cuts). The structured tree strips labels — clean folds emit zero gotos
    so this is a no-op for them — but a kept honest goto needs its target label back for valid
    C. Insert each target's label (column 0, the existing convention) before the first emitted
    line of that target's block. The gate ignores labels, so this is cosmetic-only."""
    targets = set()
    for _a, _ind, t in out:
        for m in _RE_GOTO_TGT.finditer(t):
            targets.add(int(m.group(1), 16))
    if not targets:
        return out
    # Idempotent: a label already present (from an earlier pass — the honesty-repair re-label)
    # counts as placed, so a second call never duplicates `L_xxxx:`.
    placed = {int(t.strip()[2:6], 16) for _a, _i, t in out if vm_cfg._RE_LABEL.match(t.strip())}
    result = []
    for addr, ind, text in out:
        if targets and addr and block_of(addr) in targets and block_of(addr) not in placed:
            T = block_of(addr)
            result.append((T, 0, f"L_{T:04X}:"))
            placed.add(T)
        result.append((addr, ind, text))
    return result


_RE_GOTO_ONLY = re.compile(r'^goto L_([0-9A-Fa-f]{4});\s*(//.*)?$')


def _peephole(out, validate=None):
    """CFG-neutral cleanup of the emitted lines — the caller RE-VALIDATES the result against
    the equivalence gate, so this only ever PROPOSES; an unsound proposal is rejected wholesale.

    (1) goto-to-fall-through: a standalone `goto L_X;` whose IMMEDIATELY-following emitted line
        is `L_X:` is redundant — physical fall-through already reaches X (the region reducer's
        honest-goto cuts / flat-span joins routinely emit `goto L_next; L_next:` at a boundary).
        Drop the goto. Emit-order adjacency (not address order) is the trigger, because the
        structured C is read in emit order; the gate's address-based re-check is the backstop.
        The matcher tolerates a trailing `// $addr` comment: a REAL bytecode jump carried verbatim
        from a block node (e.g. a loop-entry jump-to-condition `goto L_h; // $a` whose header `L_h`
        is the very next line, or a routing stub) keeps its provenance comment, so an anchored
        `;$` matcher would clip only the synthetic (comment-less) cuts and leave the real ones.

        Applied INCREMENTALLY when `validate(seq)->bool` is supplied: emit-order adjacency can
        disagree with the gate's re-lowering at a flat-span boundary where a block's emit-NEXT
        label is NOT its address-next leader (a REORDERED routing stub) — there the drop is
        unsound. A batch drop + one wholesale re-check reverts the WHOLE batch when any single
        clip is unsound, losing the sound ones too (e.g. a loop-entry `goto L_h; L_h:` the gate
        accepts because it understands the `while`, dropped alongside a bad stub in the same sub).
        So each candidate is validated on its own and kept only if it still passes; the unsound
        ones stay as honest gotos. Without a validator (tests), every candidate is dropped and the
        caller re-checks the batch.
    (2) then drop any `L_X:` label no surviving goto still targets (cosmetic; the gate ignores
        labels, so this is always safe)."""
    pruned = list(out)
    i = 0
    while i < len(pruned):
        m = _RE_GOTO_ONLY.match(pruned[i][2].strip())
        if m and i + 1 < len(pruned) \
                and pruned[i + 1][2].strip() == f"L_{int(m.group(1), 16):04X}:":
            trial = pruned[:i] + pruned[i + 1:]   # drop this redundant goto-to-next
            if validate is None or validate(trial):
                pruned = trial
                continue                       # re-examine the line now at index i
        i += 1
    refs = set()
    for _a, _i, t in pruned:
        for m in _RE_GOTO_TGT.finditer(t):
            refs.add(int(m.group(1), 16))
    result = []
    for addr, ind, text in pruned:
        lm = vm_cfg._RE_LABEL.match(text.strip())
        if lm and int(text.strip()[2:6], 16) not in refs:
            continue                           # dead label — no goto targets it anymore
        result.append((addr, ind, text))
    return result
