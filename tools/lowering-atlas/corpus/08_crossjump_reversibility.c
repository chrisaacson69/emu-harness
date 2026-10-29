/* 08 — CROSS-JUMP REVERSIBILITY corpus.
 *
 * The forward optimization (pinned by flag bisection): GCC's RTL cross-jumping pass
 * (-fcrossjumping, on at -O2+). It merges identical SUFFIXES of two+ blocks by pointing all
 * but one predecessor at a single surviving copy — frequently producing a BACKWARD jmp (the
 * later-addressed arm jumps into an earlier-addressed merged block = the "address-inverted
 * shared tail" the NA1 decompiler gate can't place).
 *
 * The inverse atom is TAIL DUPLICATION: copy the merged block back into each predecessor,
 * restoring the -O0 layout. This corpus grades the shapes along the ONE axis that decides
 * whether the duplicated form is ADDRESS-MONOTONIC (gate-emittable) or not: what the merged
 * block's TERMINATOR is.
 *
 *   SINK   (ends in return / tail-call)  -> duplication is provably sound AND address-ordered
 *                                           (a sink has no successor to constrain ordering).
 *   CHAIN  (falls/jmps to a further block) -> duplication recurses; emittable iff the chain
 *                                             bottoms out in a sink with no foreign predecessor.
 *   BRANCH (merged block itself branches)  -> duplication copies the branch; emittable depends.
 *   LOOP   (merged block is/reaches a back-edge) -> NOT freely duplicable.
 *
 * Compile -O0/-O1/-O2/-Os and diff (tools/probe-crossjump.py). -O0/-O1 = duplicated target;
 * -O2+ = cross-jumped. Cross-ref ../nobunaga/decompiler-atom-log.md (atom-5 / the 130-goto bucket).
 */
extern int  cond(int);
extern void msg(int);
extern void draw(int);
extern int  fin(void);      /* a terminal sink call (we `return fin();`) */
extern void cont(int);      /* a non-terminal continuation */
extern int  pick(int);      /* switch selector */
extern int  draw_n(int);

/* ---- A. SINK, 2 arms. The $AD38 baseline. Shared tail ends in return.
   Inverse: duplicate the sink into both arms (address-ordered, 0 goto). REVERSIBLE+EMITTABLE. */
int A_sink2(int a, int b) {
    if (cond(a)) { msg(1); draw(a); return fin(); }
    if (cond(b)) { msg(2); draw(b); return fin(); }
    return 0;
}

/* ---- B. SINK, N arms (guard chain to one shared return). Inverse: N copies of the sink.
   Tests whether reversibility holds as fan-in grows (the $A2D2 guard-chain family). */
int B_sinkN(int a, int b, int c, int d) {
    if (cond(a)) { msg(1); return fin(); }
    if (cond(b)) { msg(2); return fin(); }
    if (cond(c)) { msg(3); return fin(); }
    if (cond(d)) { msg(4); return fin(); }
    return 0;
}

/* ---- C. SINK via SWITCH cases (atom-4 ∩ atom-5: the $A2D2 shape — a switch whose cases
   converge on a shared terminal). Does cross-jumping merge case tails the same way? */
int C_switch_sink(int s, int a) {
    switch (pick(s)) {
        case 0: msg(1); draw(a); return fin();
        case 1: msg(2); draw(a); return fin();
        case 2: msg(3); draw(a); return fin();
        default: return 0;
    }
}

/* ---- D. CHAIN: common suffix is NON-terminal — both arms share `cont(9)` then fall to a
   second shared block that returns. Maximal common suffix = `cont(9); return fin();`, so this
   likely STILL merges to one sink (gcc merges the whole suffix). Confirms CHAIN collapses to
   SINK when the chain is fully shared. */
int D_chain_shared(int a, int b) {
    if (cond(a)) { msg(1); cont(9); return fin(); }
    if (cond(b)) { msg(2); cont(9); return fin(); }
    return 0;
}

/* ---- E. NON-SINK merge with a FOREIGN predecessor: the genuinely hard case. Block `cont(r)`
   is reached by the two cross-jumpable arms AND by a third path (else r=0), and it has a real
   successor (return fin()) — it is NOT a sink and cannot be pulled entirely into the arms.
   This is the "non-terminal shared merge" of the 130-goto residue bucket. */
int E_nonsink_merge(int a, int b) {
    int r;
    if (cond(a))      { msg(1); draw(7); r = 1; }
    else if (cond(b)) { msg(2); draw(7); r = 2; }
    else              { r = 0; }
    cont(r);                 /* shared merge: in-degree 3, has a successor -> NON-sink */
    return fin();
}

/* ---- F. LOOP tail: the merged suffix sits inside / reaches a loop back-edge. Duplicating a
   block that participates in a cycle is not a free sink-dup. Probes the loop boundary. */
int F_loop_tail(int a, int n) {
    int i, r = 0;
    for (i = 0; i < n; i++) {
        if (cond(a + i)) { msg(1); r += draw_n(i); }   /* two paths through the body... */
        else             { msg(2); r += draw_n(i); }   /* ...sharing `r += draw_n(i)` tail */
    }
    return r;
}

/* ---- G. PARTIAL overlap: arms share only a SUFFIX of differing length (one has an extra
   leading stmt). Tests that cross-jumping merges the MAXIMAL common suffix, not whole blocks —
   so the inverse must duplicate exactly the merged suffix, not the entire arm. */
int G_partial(int a, int b) {
    if (cond(a)) { msg(1); draw(a); draw(0); return fin(); }
    if (cond(b)) {         draw(b); draw(0); return fin(); }   /* shares `draw(0); return fin();` */
    return 0;
}
