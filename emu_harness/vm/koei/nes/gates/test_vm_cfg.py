"""
tools/test-vm-cfg.py — non-vacuousness tests for the CFG equivalence relation.

A gate that always says "equivalent" is worthless. These tests prove the relation
(vm_cfg.structured_equivalent / contract) actually REJECTS a control-flow change:
inject a misroute into a real sub's structured form and assert the gate flags it,
while the untouched form passes.

The structured forms are the region reducer's (vm_decompile.region_reduce), because
structured_equivalent is the relation that judges it: the reducer is DREAM's per-sub fallback,
and DREAM's own output is judged by dream_equivalent_ast in cfg_gate. Until 2026-11-02 the
forms came from the template structurer, removed then.

Run: py -3.14 -m emu_harness.vm.koei.nes.gates.test_vm_cfg
"""
import io
import re
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from emu_harness.vm.koei.nes import dream
from emu_harness.vm.koei.nes import vm_cfg
from emu_harness.vm.koei.nes import vm_decompile
from emu_harness.vm.koei.nes.cli import decompile_all

_BANKS = {}


def _grab(bank, sub):
    """One sub's collect dict: the raw witness + instructions, and `structured` = the region
    reducer's fold of it. Each bank is decompiled once, unstructured."""
    if bank not in _BANKS:
        got = {}
        saved, vm_decompile.STRUCTURE = vm_decompile.STRUCTURE, False
        try:
            with tempfile.TemporaryDirectory() as td:
                with redirect_stdout(io.StringIO()):
                    decompile_all.bank_subs(bank, Path(td), on_sub=lambda s, c: got.__setitem__(s, c))
        finally:
            vm_decompile.STRUCTURE = saved
        _BANKS[bank] = got
    cap = dict(_BANKS[bank].get(sub) or {})
    assert cap, f"sub ${sub:04X} not found in bank {bank}"
    cap['structured'] = vm_decompile.region_reduce(cap['raw'], cap['instructions'])
    return cap


def _retarget_one_goto(lines, leaders):
    """Return a copy of `lines` with the FIRST goto/if-goto retargeted to a different
    leader (a deliberate misroute), plus (old, new) for reporting. None if no goto."""
    leader_set = [L for L in leaders]
    for i, (addr, ind, text) in enumerate(lines):
        m = re.search(r'goto L_([0-9A-Fa-f]{4});', text)
        if not m:
            continue
        old = int(m.group(1), 16)
        new = next((L for L in leader_set if L != old), None)
        if new is None:
            continue
        mutated = list(lines)
        mutated[i] = (addr, ind, text.replace(f"L_{old:04X}", f"L_{new:04X}"))
        return mutated, old, new
    return None, None, None


def main():
    passed = failed = 0

    def check(name, cond):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}")

    # A couple of real subs that carry structure (if/else, loops).
    for bank, sub in [(1, 0x87F0), (0, 0xA778), (15, 0xCF98)]:
        cap = _grab(bank, sub)
        _bc, leaders = vm_cfg.bytecode_cfg(cap['instructions'])
        raw, struct = cap['raw'], cap['structured']

        # 1) The real (gated) structured form must be judged equivalent to the witness.
        ok, _, _ = vm_cfg.structured_equivalent(raw, struct, leaders)
        check(f"b{bank}/${sub:04X}: emitted structured ~= raw", ok)

        # 2) A deliberate misroute in the RAW form must be REJECTED (proves non-vacuous).
        mutated, old, new = _retarget_one_goto(raw, leaders)
        if mutated is not None:
            bad, _, _ = vm_cfg.structured_equivalent(raw, mutated, leaders)
            check(f"b{bank}/${sub:04X}: misroute L_{old:04X}->L_{new:04X} REJECTED", not bad)

        # 3) Identity must always pass (sanity that the relation isn't trivially false).
        same, _, _ = vm_cfg.structured_equivalent(raw, raw, leaders)
        check(f"b{bank}/${sub:04X}: raw ~= raw (identity)", same)

    # Phase 1 (loop folding): subs whose fold emits a real `while`.
    # The gate must (a) accept the genuine fold and (b) reject a loop turned into a
    # non-loop — replacing `while (` with `if (` deletes the back edge, a CFG change
    # the contraction relation MUST catch (else loops could silently degrade to ifs).
    for bank, sub in [(0, 0x947A), (0, 0x9E9D), (0, 0xA742)]:
        cap = _grab(bank, sub)
        _bc, leaders = vm_cfg.bytecode_cfg(cap['instructions'])
        raw, struct = cap['raw'], cap['structured']
        has_while = any('while (' in t for _a, _i, t in struct)
        check(f"b{bank}/${sub:04X}: folded a while", has_while)

        ok, _, _ = vm_cfg.structured_equivalent(raw, struct, leaders)
        check(f"b{bank}/${sub:04X}: while fold ~= raw", ok)

        deloop = [(a, i, t.replace('while (', 'if (', 1)) for a, i, t in struct]
        bad, _, _ = vm_cfg.structured_equivalent(raw, deloop, leaders)
        check(f"b{bank}/${sub:04X}: while->if (back edge dropped) REJECTED", not bad)

    # Phase 1b (do-while): bottom-test loops + self-loops -> do { body } while (C);.
    # Corrupting the `} while (C);` tail into a plain `}` deletes the back edge — the
    # relation MUST reject it (else a do-while could silently lose its loop).
    # $8C61 (draw_unit_roster_columns, loop header $8C88) is the INTERLEAVED-PURE-EXIT
    # variant: a do-while whose listing span contains an unrelated exit-path block left
    # IN PLACE (not relocated). Its `fold ~= raw` check is the regression guard for that
    # handling — it FAILED before the in-place fix (a relocated block broke the
    # address-based if/else merge computation).
    for bank, sub in [(2, 0x885E), (1, 0x8B8F), (15, 0xCD20), (2, 0x8C61)]:
        cap = _grab(bank, sub)
        _bc, leaders = vm_cfg.bytecode_cfg(cap['instructions'])
        raw, struct = cap['raw'], cap['structured']
        has_do = any(t.strip() == 'do {' for _a, _i, t in struct)
        check(f"b{bank}/${sub:04X}: folded a do-while", has_do)

        ok, _, _ = vm_cfg.structured_equivalent(raw, struct, leaders)
        check(f"b{bank}/${sub:04X}: do-while fold ~= raw", ok)

        deloop = [(a, i, ('}' if t.strip().startswith('} while (') else t))
                  for a, i, t in struct]
        bad, _, _ = vm_cfg.structured_equivalent(raw, deloop, leaders)
        check(f"b{bank}/${sub:04X}: do-while tail dropped REJECTED", not bad)

    # Phase 2 step 0 (true/false edge labels): a guard INVERSION that does NOT move the
    # bodies — `if (C) {` -> `if (!(C)) {` — runs the then/else bodies on the wrong guard.
    # The unlabelled successor SET is unchanged, so contract() alone would miss it; only
    # the orientation labels catch it. These subs have a non-empty if-header (an
    # `if (C) {} else {}` with both arms empty IS a genuine no-op under inversion, so the
    # relation correctly accepts that — excluded here).
    for bank, sub in [(0, 0x862B), (0, 0x8C45), (1, 0x87F0)]:
        cap = _grab(bank, sub)
        _bc, leaders = vm_cfg.bytecode_cfg(cap['instructions'])
        raw, struct = cap['raw'], cap['structured']
        inv, ci = None, None
        for i, (a, ind, t) in enumerate(struct):
            m = re.match(r'if \((.+)\) \{$', t.strip())
            if m:
                inv = list(struct)
                inv[i] = (a, ind, t.replace(f"if ({m.group(1)}) {{",
                                            f"if (!({m.group(1)})) {{"))
                ci = i
                break
        check(f"b{bank}/${sub:04X}: has an if-header to invert", inv is not None)
        if inv is not None:
            bad, _, _ = vm_cfg.structured_equivalent(raw, inv, leaders)
            check(f"b{bank}/${sub:04X}: guard inversion (then/else swap) REJECTED", not bad)

    # Phase 2 (break / continue): an in-loop goto to the loop exit becomes `break;`.
    # The relation must (a) accept the genuine break fold and (b) reject break turned
    # into continue — they route to DIFFERENT blocks (exit vs header), so swapping them
    # is a real control change the new loop-context lowering MUST catch (else a break
    # could silently become an infinite-loop continue, or vice-versa).
    for bank, sub in [(0, 0x9778), (0, 0x9974)]:
        cap = _grab(bank, sub)
        _bc, leaders = vm_cfg.bytecode_cfg(cap['instructions'])
        raw, struct = cap['raw'], cap['structured']
        has_break = any(re.search(r'\bbreak;', t) for _a, _i, t in struct)
        check(f"b{bank}/${sub:04X}: emitted a break", has_break)

        ok, _, _ = vm_cfg.structured_equivalent(raw, struct, leaders)
        check(f"b{bank}/${sub:04X}: break fold ~= raw", ok)

        swapped = [(a, i, re.sub(r'\bbreak;', 'continue;', t)) for a, i, t in struct]
        bad, _, _ = vm_cfg.structured_equivalent(raw, swapped, leaders)
        check(f"b{bank}/${sub:04X}: break->continue (exit->header) REJECTED", not bad)

    # Phase 2 (multi-exit loops): a loop with >1 pure-exit target -- one structural exit
    # (the while-condition false branch) PLUS mid-body `return`s that stay in the body.
    # The relation must accept the fold; and a mid-body return moved OUT of the loop
    # (deleted from the body) must be REJECTED, proving the return's EXIT edge is checked.
    for bank, sub in [(2, 0x912B)]:
        cap = _grab(bank, sub)
        _bc, leaders = vm_cfg.bytecode_cfg(cap['instructions'])
        raw, struct = cap['raw'], cap['structured']
        # locate a `return` line strictly inside the while braces (depth > 0)
        depth, inner_ret = 0, None
        for j, (a, ind, t) in enumerate(struct):
            ts = t.strip()
            if ts.endswith('{'):
                depth += 1
            elif ts == '}' or vm_cfg._RE_DOWHILE_CLOSE.match(ts):
                depth -= 1
            elif depth > 0 and re.match(r'(if \(.+\) )?return\b', ts):
                inner_ret = j
        check(f"b{bank}/${sub:04X}: has a mid-loop return", inner_ret is not None)

        ok, _, _ = vm_cfg.structured_equivalent(raw, struct, leaders)
        check(f"b{bank}/${sub:04X}: multi-exit fold ~= raw", ok)

        if inner_ret is not None:                 # delete the mid-loop return -> CFG changes
            dropped = [ln for k, ln in enumerate(struct) if k != inner_ret]
            bad, _, _ = vm_cfg.structured_equivalent(raw, dropped, leaders)
            check(f"b{bank}/${sub:04X}: mid-loop return deletion REJECTED", not bad)

    # Phase 2 (while(1) soundness / the body-tail fall model): a loop with a CONDITIONAL
    # back edge `if (C) goto L_h` is a do-while, NOT an infinite loop. Mis-folding it as
    # `while (1) { ...; if (C) continue; }` makes the fall-through (the do-while EXIT) wrongly
    # loop. The gate must REJECT this — it did NOT before the body-tail fall fix (the tail's
    # fall was modelled as next_leader, fabricating the exit edge and matching the bytecode).
    def _wrap_as_bad_while1(raw):
        """Wrap a backward `if (C) goto L_h ... L_h:` loop as while(1) with the tail
        rendered `if (C) continue;` (the unsound fold). Returns the mutated lines or None."""
        labels = {}
        for i, (a, ind, t) in enumerate(raw):
            m = re.match(r'L_([0-9A-Fa-f]{4}):$', t.strip())
            if m:
                labels[m.group(1).upper()] = i
        for i, (a, ind, t) in enumerate(raw):
            m = re.match(r'if \((.+)\) goto L_([0-9A-Fa-f]{4});$', t.strip())
            if m and labels.get(m.group(2).upper(), i) < i:   # backward = a loop back edge
                lo, cond = labels[m.group(2).upper()], m.group(1)
                out = []
                for k, (aa, ii, tt) in enumerate(raw):
                    if k == lo:
                        out.append((0, ind, "while (1) {"))
                    if k == i:
                        out.append((aa, ii + 1, f"if ({cond}) continue;"))
                        out.append((0, ind, "}"))
                    elif lo <= k <= i:
                        out.append((aa, ii + 1, tt))
                    else:
                        out.append((aa, ii, tt))
                return out
        return None

    for bank, sub in [(2, 0x830B)]:                 # consume_daily_battle_rice: do-while
        cap = _grab(bank, sub)
        _bc, leaders = vm_cfg.bytecode_cfg(cap['instructions'])
        bad = _wrap_as_bad_while1(cap['raw'])
        check(f"b{bank}/${sub:04X}: built a bad while(1)", bad is not None)
        if bad is not None:
            rej, _, _ = vm_cfg.structured_equivalent(cap['raw'], bad, leaders)
            check(f"b{bank}/${sub:04X}: cond-back-edge as while(1) REJECTED", not rej)

    # Phase 2 step 3 (two-test "both" loops): header AND tail both 2-way -> a do-while
    # whose HEADER block carries an exit-test `if (C) break;` (the top guard) while the
    # tail carries the `} while (C);` (the bottom test). Folded as a do-while; the header's
    # break is the new case — it lives in the do-HEADER block, which the lowering models
    # only because loop_ctx now KEEPS the header (the vm_cfg fix). The relation must
    # (a) accept the genuine fold and (b) reject the header break turned into a continue:
    # that routes header->header (an infinite loop) instead of header->exit, dropping the
    # header's exit edge — exactly the CFG divergence the loop_ctx-header fix repairs.
    # $D351 (ui_helper_d351) carries TWO header breaks (proves multiple header exits
    # resolve). $822A is the archetype with a HOISTED pre-loop flushed call before `do {`.
    for bank, sub in [(2, 0x822A), (15, 0xD3A7), (15, 0xD351)]:
        cap = _grab(bank, sub)
        _bc, leaders = vm_cfg.bytecode_cfg(cap['instructions'])
        raw, struct = cap['raw'], cap['structured']
        has_do = any(t.strip() == 'do {' for _a, _i, t in struct)
        has_hdr_break = any(re.match(r'if \(.+\) break;$', t.strip()) for _a, _i, t in struct)
        check(f"b{bank}/${sub:04X}: folded two-test do-while + header break", has_do and has_hdr_break)

        ok, _, _ = vm_cfg.structured_equivalent(raw, struct, leaders)
        check(f"b{bank}/${sub:04X}: two-test fold ~= raw", ok)

        swapped = [(a, i, re.sub(r'\bbreak;', 'continue;', t)) for a, i, t in struct]
        bad, _, _ = vm_cfg.structured_equivalent(raw, swapped, leaders)
        check(f"b{bank}/${sub:04X}: header break->continue (exit->header) REJECTED", not bad)

    # Value-diamond TERNARY folding: a 2-way head whose arms each just load a value and
    # converge folds to `cond ? a : b`; vm_cfg.bytecode_cfg CONTRACTS the same diamond so
    # the witness gate stays green. The relation must (a) find the diamond, (b) emit a `?:`,
    # (c) pass the witness with the CONTRACTED cfg, and (d) — the non-vacuousness — FAIL the
    # witness against the UNcontracted cfg, proving the contraction is load-bearing (the
    # diamond really is a branch in the bytecode that the fold legitimately collapses; the
    # ternary's VALUE/polarity is checked separately by the oracle probe-ternary-folds.py,
    # since the symmetric contraction makes it invisible to this structural gate).
    for bank, sub in [(15, 0xCB5E), (15, 0xCB6F), (0, 0xA778)]:   # min_word, max_word, vm_bootstrap
        cap = _grab(bank, sub)
        ins = cap['instructions']
        diamonds = vm_cfg.value_diamonds(ins)
        check(f"b{bank}/${sub:04X}: value_diamond detected", len(diamonds) >= 1)
        has_tern = any(re.search(r'\? .+ : .+', t) for _a, _i, t in cap['structured'])
        check(f"b{bank}/${sub:04X}: emitted a ?: ternary", has_tern)

        cfg_c, leaders_c = vm_cfg.bytecode_cfg(ins)            # contracted
        cfg_raw, _lr = vm_cfg._bytecode_cfg_raw(ins)           # un-contracted
        ok_c = vm_cfg.lower_goto_cfg(cap['raw'], leaders_c) == cfg_c
        check(f"b{bank}/${sub:04X}: witness ~= CONTRACTED bytecode cfg", ok_c)
        # Same folded witness must NOT match the un-contracted cfg -> contraction is needed.
        bad = vm_cfg.lower_goto_cfg(cap['raw'], sorted(cfg_raw)) == cfg_raw
        check(f"b{bank}/${sub:04X}: witness != UNcontracted cfg (contraction load-bearing)", not bad)

    # Short-circuit BOOLEAN regions (K>=2 conditions -> `c1 && c2 … ? a : b`): same contract
    # contract that the simple diamond gets, but over a multi-block region. Same checks:
    # region detected, a &&/|| compound emitted, witness ~= CONTRACTED and != UNcontracted.
    # (is_tile_in_bounds $8F11 = a 2-cond &&; $9714 = the 4-cond combat-arena bounds check.)
    for bank, sub in [(2, 0x8F11), (2, 0x970A)]:
        cap = _grab(bank, sub)
        ins = cap['instructions']
        regions = vm_cfg.boolean_regions(ins)
        check(f"b{bank}/${sub:04X}: boolean region detected", len(regions) >= 1)
        has_bool = any(re.search(r'&&|\|\|', t) for _a, _i, t in cap['structured'])
        check(f"b{bank}/${sub:04X}: emitted a &&/|| compound", has_bool)

        cfg_c, leaders_c = vm_cfg.bytecode_cfg(ins)
        cfg_raw, _lr = vm_cfg._bytecode_cfg_raw(ins)
        ok_c = vm_cfg.lower_goto_cfg(cap['raw'], leaders_c) == cfg_c
        check(f"b{bank}/${sub:04X}: witness ~= CONTRACTED bytecode cfg", ok_c)
        bad = vm_cfg.lower_goto_cfg(cap['raw'], sorted(cfg_raw)) == cfg_raw
        check(f"b{bank}/${sub:04X}: witness != UNcontracted cfg (contraction load-bearing)", not bad)

    # Phase 1c (SIBLING loops): a sub with >1 DISJOINT loop header folds each loop
    # independently (each gated against the raw witness on its own, so one bad sibling fold
    # can't sink the others). $A3AA (write_sram_save_checksum) = two sequential do-while loops
    # (checksum SRAM $6100-$6F00, then $7100-$7E00). The relation must (a) accept >= 2 folded
    # loops, and (b) reject dropping just ONE loop's back edge — proving the second fold does
    # NOT mask a corruption of the first (the per-sibling independence the gating relies on).
    for bank, sub in [(0, 0xA3AA), (0, 0xA455)]:
        cap = _grab(bank, sub)
        _bc, leaders = vm_cfg.bytecode_cfg(cap['instructions'])
        raw, struct = cap['raw'], cap['structured']
        nloops = sum(1 for _a, _i, t in struct if ('while (' in t or t.strip() == 'do {'))
        check(f"b{bank}/${sub:04X}: folded >=2 sibling loops", nloops >= 2)

        ok, _, _ = vm_cfg.structured_equivalent(raw, struct, leaders)
        check(f"b{bank}/${sub:04X}: sibling folds ~= raw", ok)

        # Drop just the FIRST loop's back edge (a `} while(C);` -> `}`, or `while(C){` ->
        # `if(C){`) and assert the gate REJECTS it — the other sibling fold must not hide it.
        mut, done = [], False
        for a, i, t in struct:
            ts = t.strip()
            if not done and vm_cfg._RE_DOWHILE_CLOSE.match(ts):
                mut.append((a, i, '}')); done = True
            elif not done and re.match(r'while \(.+\) \{$', ts):
                mut.append((a, i, t.replace('while (', 'if (', 1))); done = True
            else:
                mut.append((a, i, t))
        check(f"b{bank}/${sub:04X}: found a sibling loop to corrupt", done)
        if done:
            bad, _, _ = vm_cfg.structured_equivalent(raw, mut, leaders)
            check(f"b{bank}/${sub:04X}: one sibling loop's back edge dropped REJECTED", not bad)

    # Phase 3 (switch case-body inlining): a single-entry switch's case bodies move INSIDE
    # the braces as a native C switch (inline `case N:` labels + honest internal goto where
    # fall-through can't reach). The switch HEADER's successors are read from those inline
    # labels, so DELETING a case label drops a real dispatch edge (or, for a `default:`,
    # flips has_default and fabricates a fall-through) — a CFG change the gate MUST catch,
    # else a switch could silently lose a case. $A30D = the season dispatcher (goto into a
    # shared body); $DE78 = a message selector whose merge is reached by goto; $9BB4 = a
    # FALL-THROUGH LADDER (`terrain = 3 - count`) whose `default` is an empty trailing case
    # falling through the `}` to the trailing merge (the step-2 break-by-fall-through case).
    for bank, sub in [(0, 0xA30D), (1, 0xB09D), (2, 0x9BB4), (15, 0xDE78)]:
        cap = _grab(bank, sub)
        _bc, leaders = vm_cfg.bytecode_cfg(cap['instructions'])
        raw, struct = cap['raw'], cap['structured']
        block_of = vm_cfg._block_of_fn(leaders)
        has_case = any(re.match(r'(?:case -?\d+|default):$', t.strip()) for _a, _i, t in struct)
        check(f"b{bank}/${sub:04X}: inlined switch case bodies", has_case)

        ok, _, _ = vm_cfg.structured_equivalent(raw, struct, leaders)
        check(f"b{bank}/${sub:04X}: switch inline ~= raw", ok)

        # Dropping a case whose target is a pure router shared by a sibling case is genuinely
        # CFG-neutral (contraction absorbs it — the gate is RIGHT to accept that). So the
        # non-vacuousness claim is EXISTENTIAL: at least one unique-target case exists whose
        # deletion the gate REJECTS (proving a lost dispatch edge is caught, not waved through).
        tgt_count, case_idx = {}, {}
        for i, (a, _ind, t) in enumerate(struct):
            if re.match(r'(?:case -?\d+|default):$', t.strip()):
                b = block_of(a)
                tgt_count[b] = tgt_count.get(b, 0) + 1
                case_idx.setdefault(b, i)
        rejected_any = False
        for b, c in tgt_count.items():
            if c != 1:
                continue
            dropped = [ln for k, ln in enumerate(struct) if k != case_idx[b]]
            bad, _, _ = vm_cfg.structured_equivalent(raw, dropped, leaders)
            if not bad:
                rejected_any = True
                break
        check(f"b{bank}/${sub:04X}: a dropped case (dispatch edge lost) is REJECTED", rejected_any)

    # DREAM's relation (dream_equivalent_ast), which judges every DREAM-owned sub in cfg_gate:
    # the same non-vacuousness, on DREAM's AST. The loops here (menu while+continue, while(1)
    # with a conditional exit, a multi-exit loop) were the template structurer's specimens above
    # until 2026-11-02; DREAM is what structures them now. Each AST must pass, and each mutation
    # must be REJECTED: an if's arms swapped (the guard runs the wrong body), a break turned
    # into the loop's continue (exit -> header), a continue into a break (header -> exit).
    def _first(ast, kind, in_loop=False):
        """The first node of `kind` (depth-first) with the header of the loop enclosing it. With
        in_loop, only a node whose innermost enclosing loop/switch is the loop: a break inside a
        switch leaves the switch (to its merge, which goes on round the loop), not the loop."""
        def walk(nodes, h, inner_kind):
            for n in nodes:
                if not (isinstance(n, tuple) and n and isinstance(n[0], str)):
                    continue
                if n[0] == kind and (not in_loop or inner_kind == 'loop'):
                    return n, h
                h2 = n[1] if n[0] == 'loop' else h
                k2 = n[0] if n[0] in ('loop', 'switch') else inner_kind
                for e in n[1:]:
                    if isinstance(e, list):
                        got = walk(e, h2, k2)
                        if got:
                            return got
            return None
        return walk(ast, None, None)

    def _replace(ast, old, new):
        """A copy of the AST with node `old` (by identity) replaced by `new`."""
        def walk(x):
            if x is old:
                return new
            if isinstance(x, list):
                return [walk(y) for y in x]
            if isinstance(x, tuple) and x and isinstance(x[0], str):
                return tuple(walk(e) if isinstance(e, list) else e for e in x)
            return x
        return walk(ast)

    for bank, sub in [(2, 0x8669), (2, 0x86F9), (1, 0xB1A6), (1, 0xB6B4), (1, 0xAB22), (0, 0x8FF8)]:
        cap = _grab(bank, sub)
        built = dream.dream_ast(cap['raw'], cap['instructions'])
        check(f"b{bank}/${sub:04X}: DREAM builds an AST", built is not None)
        if built is None:
            continue
        ast, tc, _nxt = built

        def accept(a, cap=cap, tc=tc):
            return dream.dream_equivalent_ast(cap['raw'], a, tc, tc['leaders'])
        check(f"b{bank}/${sub:04X}: DREAM AST ~= raw", accept(ast))
        base_cfg = dream._ast_cfg(ast, tc)

        # A mutation the AST's own CFG does not see is neutral (e.g. a continue after a switch
        # whose merge is the loop header is never reached), so it proves nothing either way:
        # only a CFG-changing mutation must be REJECTED, and each sub must have one.
        mutations = []
        got = _first(ast, 'if')
        if got:
            n = got[0]
            mutations.append(("if arms swapped", _replace(ast, n, n[:5] + (n[6], n[5]))))
        brk, cont = _first(ast, 'break', in_loop=True), _first(ast, 'continue')
        if brk and brk[1] is not None:
            mutations.append(("break->continue (exit->header)", _replace(ast, brk[0], ('continue', brk[1]))))
        if brk and cont:
            mutations.append(("continue->break (header->exit)", _replace(ast, cont[0], brk[0])))
        live = 0
        for what, mut in mutations:
            if dream._ast_cfg(mut, tc) == base_cfg:
                print(f"  (neutral, skipped)  b{bank}/${sub:04X}: DREAM {what}")
                continue
            live += 1
            check(f"b{bank}/${sub:04X}: DREAM {what} REJECTED", not accept(mut))
        check(f"b{bank}/${sub:04X}: DREAM has a CFG-changing mutation", live > 0)

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
