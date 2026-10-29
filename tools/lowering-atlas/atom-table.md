---
status: active
created: 2026-06-05
---
# The Atom Table — C construct → CFG signature (forward-sourced)
> The finite inverse-lowering table the [nobunaga](https://github.com/chrisaacson69/na1-decompiler/tree/main/nobunaga) decompiler is discovering one
> failing sub at a time, sourced the cheap way: compile each construct and read the jump pattern.
> Each row is one *forward* lowering; its inverse is a candidate pre-contraction atom in
> `emu_harness/vm/koei/nes/vm_reduce.py`. Cross-checked against [decompiler-atom-log](https://github.com/chrisaacson69/na1-decompiler/blob/main/nobunaga/decompiler-atom-log.md).

**Provenance.** GCC 15.2.0 (msys2 ucrt64), `-O0 -fdump-tree-cfg` (GIMPLE CFG, pre-optimization).
Regenerate: `py -3 tools/compile-dumps.py && py -3 tools/cfg-signature.py`. Corpus `corpus/*.c`.

**Caveat (not NA1's compiler).** These are GCC's GIMPLE lowerings — the *space* and *invariants* of
structured→jump shapes, not NA1's exact opcodes. The value is recognizing a construct from its CFG
fingerprint and knowing which inversions are real (one shape, many constructs) vs which collapse
ambiguously (one shape, can't tell which construct — see "Graph-indistinguishable").

## Signature notation

`<kind><id>>‹succs›` per block, DFS-ordered from entry, bb-numbers stripped. kind: `b`=2-way branch,
`s`=n-way switch, `r`=return-to-EXIT, `l`=linear/merge/body. `X`=virtual EXIT. `^n`=back-edge to n.
So `b0>1,2 l1>2 l2>3 r3>X` = "branch → {then, fall}, both reach a merge, merge returns" = a plain `if`.

## The table

| # | Construct | Fingerprint (canonical) | The identifiable mark | NA1 cross-ref |
|---|---|---|---|---|
| **Conditionals** |
| if | `c_if` | `b0>1,2 l1>2 l2>3 r3>X` | one 2-way branch, both arms reach a shared merge; one arm has the body | — |
| if/else | `c_if_else` | `b0>1,4 l1>2 l2>3 r3>X l4>2` | branch → two bodied arms → shared merge; the then-arm has a `goto merge` (else falls) | — |
| else-if chain | `c_elseif_chain` | `…b4>5,6…b6>7,8…` (staircase) | a chain of 2-way branches where every *false* edge feeds the next test, every *true* arm jumps to one shared merge | — |
| **guard (early return)** | `c_guard` | `b0>1,3 l1>2 r2>X l3>2` | a 2-way branch one of whose arms is a **terminal** (return) block | **atom-4-adjacent**: branch-to-terminal ⇒ guard |
| **guard chain → shared return** | `c_guard_chain` | `…r2>X…` in-deg≥2 | N guards each branching to **one shared return block** (the `SHARED-RETURN` flag) | **the `$A2D2`/`$AD38` family** — top-down's "cross-edge"; really a missing guard atom (thesis §"goto metric lied") |
| **Loops** |
| while (pre-test) | `l_while` | `l0>1 b1>2,3 l2>^1 l3>4 r4>X` | preheader **jumps forward** to a test header (2-way); body is the latch; **one back-edge** latch→header | base loop shape |
| do-while (post-test) | `l_do_while` | `l0>1 b1>^1,2 …` | the header **is** the test+latch (self back-edge); body falls into it | — |
| for | `l_for` | `l0>1 b1>2,3 l2>^1 l3>4 r4>X` | **identical to `while`** — init in preheader, update in latch-tail | NA1 names these by stmt position, not shape |
| infinite + internal break | `l_for_infinite` | `…ROUTING…` | header has no exit test; exit is a `break` edge via a routing stub | — |
| **Loop jumps** |
| break | `j_while_break`/`j_for_break` | `…b2>3,6…r3>4…l4>5…` + ROUTING | an exit edge to the loop's exit block that does **not** go through the test; appears as a routing stub forwarding to the post-loop block | — |
| **continue** | `j_while_continue` | loop reports **`hdr None, latch None`** | a **second back-edge** to the header (multi-latch) — GCC's own latch detector returns None | **atom-1** (multi-latch `while(1)`-with-continues) — confirmed forward |
| **multi-level break (goto)** | `j_multi_level_break` | inner break edge targets a block **outside the parent loop** (`l8>^3` + `b3>4,9` escaping) | a single edge escaping ≥2 loop nests to a shared target; C has no 2-level break so the source used `goto` | **atom-2** (multi-level break = honest goto) — confirmed forward |
| nested + inner break | `j_nested_break_inner` | two real loops (depth 1, 2), break stays in inner | single-level break = inner-exit routing stub | — |
| **Switch** (NA1's biggest lever) |
| dense | `s_dense` | `s0>1,4,5,6,7 …` all cases → one merge | n-way block; succ order **[default, case0, case1, …]** (default first); cases → shared merge M | **matches `$D5` "default before case targets"** |
| sparse | `s_sparse` | same family, fewer/uneven keys | same CFG shape — GCC's table-vs-if-chain choice is invisible at GIMPLE-CFG level | `$D9` noncontiguous |
| fall-through | `s_fallthrough` | `…l4>5 l5>2…` (case body falls into next) | cases without `break` fall into the next case body → chained merges | NA1 cases fall through C-style |
| case-inline (no breaks) | `s_case_inline` | staircase of merges `l3>4 l4>5 l5>1` | a single straight-line run entered at different points; every case is a merge | **switch-case-inline atom** candidate |
| default in middle | `s_default_middle` | default edge still listed **first** in succs | source order of `default` does not change its succ-position (always first) | confirms default-first is structural |
| **switch + shared merge M** | `sm_guard_shares_merge` | `…r2>X` (guard) + `s3>…` cases → bb9 → **shared return bb10** | **M = post-dom(S) is shared**: reached by the cases *and* a pre-switch guard → M's in-degree > #cases → breaks "M single-entry-dominated by S" | **atom-4 ROOT** — exactly `$9C84`; the immediate case-merge is clean, the *post-dominator* is shared |
| switch + pre-block share | `sm_default_preswitch_share` | default routes via stub to a merge a pre-switch block also reaches | interleaved/shared default merge | **`$9ED9`** interleaved-default |
| **Short-circuit & value** |
| `if(a && b)` | `b_and` | `b0>1,3 b1>2,3 l2>3 …` | two chained branches whose **false** edges both target the same merge; both-true reaches the body | value-diamond / boolean-region (NA1 locked-decision #4) |
| `if(a \|\| b)` | `b_or` | `b0>1,4 … b4>1,2` | first branch's **true** edge short-circuits **into** the then-block; false falls to the next test | boolean-region |
| `(a&&b)\|\|c` | `b_mixed` | 3 branches, nested diamonds | composition of the above two shapes | boolean-region |
| ternary `?:` | `b_ternary` | `b0>1,4 l1>2 l2>3 r3>X l4>2` | **identical to if/else** feeding one assignment | value-diamond |
| `&&` as rvalue | `b_and_value` | `b0>1,5 b1>2,5 …l5>3` | the diamond computes a 0/1 value, not control | value-diamond |

## Graph-indistinguishable pairs (the honest limit of CFG-shape matching)

Same fingerprint ⇒ the construct is **not** recoverable from graph structure alone; you need
statement positions. The decompiler should pick the canonical/simplest and not invent a handler to
tell them apart:

- **`for` ≡ `while`** — `l0>1 b1>2,3 l2>^1 l3>4 r4>X`. Distinguish only by: init in preheader +
  update as the last latch statement ⇒ render `for`, else `while`.
- **ternary `?:` ≡ `if/else`-assigning-one-var** — `b0>1,4 l1>2 l2>3 r3>X l4>2`. Distinguish only by
  whether both arms assign the *same* destination then it's immediately used (⇒ `?:`).

## What this confirms for NA1 (the cross-check)

1. **Atoms 1 & 2 are real forward lowerings, not 1-offs.** `continue` reproducibly makes GCC's own
   latch-finder return `None` (multi-latch); a `goto` out of nested loops reproducibly escapes the
   parent loop. Both are *constructs*, so their inverses are atoms. ✔ (matches landed atoms 1, 2.)
2. **The guard / shared-return family is one atom, not a tangle.** `c_guard` + `c_guard_chain` show
   the thesis's claim concretely: N branches to a shared **terminal** is a guard chain, and a terminal
   can be duplicated freely — so the "cross-edge merge" top-down treated as sacred dissolves. The
   inverse atom is *branch-to-terminal ⇒ guard*. This is the next atom to write (clears the
   `shared-ret>0` cluster: `$9A5D`, `$AD38`, part of `$A2D2`).
3. **Atom-4 is precisely a shared post-dominator, confirmed forward.** `sm_guard_shares_merge`
   reproduces `$9C84`: the switch's *immediate* case-merge is single-entry-clean, but `M=post-dom(S)`
   (the shared return) has in-degree > #cases because a guard also reaches it. ⇒ The fix is to model
   `M=post-dom(S)` as the switch EXIT and **exclude it from the case-target/enclosed-domination
   checks** — exactly the atom log's plan. The breakage comes from the *guard edge into M*, not the
   cases, so excluding M (not requiring it be S-dominated) is the right and sufficient relaxation.
4. **Switch default-first is structural** (`s_dense`, `s_default_middle`) — matches `$D5`. Good
   confidence the NA1 switch model is reading the format right.

## Hand-off contract → NA1

Each row's inverse is a candidate **CFG pre-contraction** in `emu_harness/vm/koei/nes/vm_reduce.py` (rewrite
the matched subgraph to a token before the region tree runs — the bottom-up move, same as the existing
value-diamond/boolean-region pre-contractions). The procedure ([thesis](https://github.com/chrisaacson69/na1-decompiler/blob/main/nobunaga/decompiler-bottom-up-thesis.md)):
write the inverse → delete the rung-6 handler it obsoletes → re-run `probe-cfg-reducibility.py` (a real
atom clears several subs). This table tells you **which inversions exist** so you stop discovering them
one-failing-sub-at-a-time and know when the table is closed.

**Next, by leverage:** (a) **branch-to-terminal guard** (this table row `c_guard_chain`) — clears the
shared-return cluster; (b) **atom-4 shared-switch-exit** (`sm_guard_shares_merge`) — 365 gotos / 36 subs.

## -O0 vs -O2: base lowerings vs inverse-OPTIMIZATIONS (2026-06-05)
> `corpus/07_terminal_merge.c` + `tools/probe-opt-levels.py`. Compiling the SAME C at -O0 and -O2
> splits the NA1 decompiler atoms into two fundamentally different kinds — a distinction invisible
> from -O0 alone.

**The experiment.** Two `if`-arms with a common multi-statement tail ending in `return` (different
prefixes) — the `$AD38` shape:

| | -O0 | -O2 |
|---|---|---|
| `family2_common_tail` | tail **DUPLICATED** (one full copy per arm, address-ordered, 0 goto) | tail **MERGED** to one copy; the later arm does `jmp` **backward** into it (cross-jump) |
| `family1_guard_merge` | **single** shared tail, address-ordered (a base lowering) | cross-jumped further |

**What it clarifies:**
1. **`$AD38`'s "address-inverted shared tail" is a TAIL-MERGE / CROSS-JUMP OPTIMIZATION** — it appears
   at -O2, *never* at -O0. So the NA1 decompiler atom that recovers it (terminal duplication) **inverts
   an optimization, not a base lowering** — and the **-O0 output IS the target form** (the duplicated,
   address-ordered, goto-free version). The inverse is well-defined and provably sound: duplicating a
   *terminal* (sink) block is always CFG-preserving.
2. **NA1's compiler DID cross-jumping.** The ROM contains the merged form (`$AD38`), so NA1's toolchain
   optimized at least to the tail-merge level — it was non-optimizing for *structure* (reducibility
   holds) but applied cross-jumping. Refines the [bottom-up thesis](https://github.com/chrisaacson69/na1-decompiler/blob/main/nobunaga/decompiler-bottom-up-thesis.md)'s
   "non-optimizing compiler" to "non-optimizing **except** tail-merge/cross-jump."
3. **Two atom kinds ⇒ two decompiler strategies.** Base-lowering inverses (family-1, switch-shared-merge,
   if/while) are present at -O0 → the reducer structures them directly (a family-1 miss is a reducer
   bug). Optimization inverses (`$AD38`) need the cross-jump **undone** — a CFG pre-contraction that
   duplicates the shared terminal back to its -O0 layout.
4. **The decompiler gate's "lexical == address" invariant is exactly the -O0 assumption.** Cross-jumped
   (-O2) code violates it. So the gate change family-2 needs is **narrow**: let a *terminal* (→EXIT)
   block be emitted in multiple lexical positions (it's a sink, so it adds no ordering ambiguity for
   other blocks) — NOT a wholesale "drop lexical==address."

**Method upgrade:** the atlas now compiles at -O0 (base lowerings) AND -O2 (optimizations); the
**-O0 ↔ -O2 diff is the optimization-inverse atom set**. `tools/probe-opt-levels.py` is the harness.

## The cross-jump optimization — pinned, and its reversibility boundary mapped (2026-06-05)
> `corpus/08_crossjump_reversibility.c` + `tools/probe-crossjump.py`. The atom log's "address-inverted
> shared tail" residue (the 130-goto "neither" bucket + the terminal family) is the inverse of ONE
> named GCC pass; this section pins it and maps exactly which shapes its inverse can recover.

**The pass is RTL cross-jumping (`-fcrossjumping`, on at -O2+).** Flag bisection at -O2: `-fno-crossjumping`
alone restores the -O0 duplicated form (3 sink copies → back to 3); `-fno-tree-tail-merge` does nothing.
So the GIMPLE tail-merge pass is innocent — it is the **RTL cross-jumping** pass that merges identical
block SUFFIXES and emits the **backward `jmp`** (a later-addressed arm jumping into an earlier-addressed
surviving copy) that the NA1 gate's lexical==address invariant cannot place.

**The reversibility boundary (the headline).** Cross-jumping only ever merges a block down to a point;
the question is what the merged block's terminator is. Measured across 7 graded shapes:

| corpus shape | -O0 | -O2 | what GCC did | reversible by tail-dup? |
|---|---|---|---|---|
| `A_sink2` 2-arm sink, tiny tail | 2 copies | **2 copies** | **NOT merged** (below cross-jump size threshold) | already -O0 form |
| `B_sinkN` 4 guards → shared return | 4 | 1 | merged, **forward** edges (sink placed last) | yes; sink-dup ×N, monotonic |
| `C_switch_sink` switch cases → shared return | 3 | 1 | merged, **BACKWARD jmp** (sink placed *between* case addrs) | **yes — SINK INVERSION** |
| `D_chain_shared` 2 arms, fully-shared multi-stmt suffix | 2 | 1 | merged, forward | yes |
| `E_nonsink_merge` merge has a successor + foreign pred | 1 | **2** | **DUPLICATED, not cross-jumped** | already emittable |
| `F_loop_tail` shared body tail inside a loop | — | — | not merged (cycle block) | n/a |
| `G_partial` arms share only a maximal suffix | 2 | 1 | merged, **BACKWARD jmp** | **yes — SINK INVERSION** (dup the suffix) |

**Three findings that reframe the NA1 plan:**
1. **GCC cross-jumps SINKS only.** Every backward-jmp inversion (`C`, `G`, and `07`'s `family2`) merges a
   suffix that ends in `return`/tail-call. The one **non-sink** merge with a successor (`E`) GCC handled by
   **duplication**, never a cross-jump — so it stayed address-ordered. **Inference:** if NA1's toolchain
   is a typical cross-jumper, its entire *address-inversion* residue is SINK merges → **fully reversible by
   tail-duplication**, and the "non-terminal shared merge" part of the 130-goto bucket is **NOT a cross-jump
   artifact** (it's genuine irreducibility / address-ordered-emitter layout — investigate separately, do not
   lump it with the cross-jump inverse).

   **MEASURED (2026-06-05, NA1's `probe-inversion-sink.py` (retired; restore from na1-decompiler `fa8fc6e`)) — residue is NOT sink-dominated.**
   Classifying every NA1 residue `goto L_T` by target cone: of 221 recoverable gotos, only **44 (22%) are
   SINK** (reversible by dup), **153 (77%) are NON-SINK**, the latter overwhelmingly **forward** (341 fwd /
   70 back). Since GCC never cross-jumps a non-sink (shape `E`), those 153 are **not cross-jump artifacts**.
   So the inference's *second half holds* (the majority isn't cross-jump) but the optimistic first half is
   **refuted**: the cross-jump (sink) slice clears only ~22%. The 77% majority is the reducer/emitter
   non-terminal-shared-merge gap (fold a forward goto into a shared if-merge), not an optimization to invert.
2. **The sink inverse is provably sound AND address-monotonic.** Duplicating a →EXIT block is the literal
   inverse of cross-jumping and constrains no successor's ordering, so each copy lands at its predecessor's
   natural address. This is why the atom log's "narrow gate change" is the *right* scope — it is narrow *in
   principle*. (The atom log's `$AD38` gate-crack hit a 4-row wall because the **current** gate keys
   fall-through on the dup block's *original* address; the fix is to re-tag the duplicate to the
   predecessor's range — an implementation detail of the narrow change, not a refutation of it.)
3. **The inversion is an -O2 layout choice, not inherent to the merge.** At `-Os` the *same* merges (`C`,
   `G`) come out **forward-only** (`1c/0b`) — GCC reorders to avoid the backward jmp. So a cross-jumped CFG
   has both an emittable (forward) and a non-emittable (backward) linearization; the decompiler's job is
   purely to pick the forward one (= duplicate the sink). Reinforces: this is a *layout* inversion, fully
   recoverable, not a structural loss.

Run: `py -3 tools/probe-crossjump.py --bisect`.

## v2 (planned)

Add **cc65** (real 6502 backend, the NES CPU family; closest cultural match to a 1988 console
compiler) as a second data point — test which fingerprints are compiler-independent (the real
invariants) vs GCC-specific, and whether cc65's switch/loop idioms sit closer to NA1's observed shapes.

