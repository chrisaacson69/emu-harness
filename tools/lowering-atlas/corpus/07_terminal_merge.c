/* The two NA1 decompiler atoms, reproduced to test whether they are base lowerings or
   OPTIMIZATIONS. Compile this BOTH -O0 and -O2 and diff. (tools/compile-dumps.py is -O0;
   the opt comparison is run by hand / probe-opt-levels.) */
extern void msg(int);
extern void draw(int);
extern void redraw(int);
extern int  draw_window(void);
extern int  cond(int);
extern void work(int);

/* FAMILY-1 ($8FF8): guards fall through to a shared MULTI-STATEMENT terminal tail that sits
   AFTER them by address. The natural nesting target. Expect: -O0 keeps ONE tail block,
   address-ordered (no goto, no duplication) — i.e. exactly what the reducer should fold. */
int family1_guard_merge(int a) {
    int r = 0;
    if (cond(a)) {
        if (cond(a + 1)) {
            work(a);
            r = 1;
        }
    }
    redraw(1);            /* shared multi-stmt terminal tail, reached by fall-through */
    redraw(2);
    return draw_window();
}

/* FAMILY-2 ($AD38): two arms with a COMMON multi-statement tail ending in return, different
   prefixes. This is the cross-jump / tail-merge shape. Expect: -O0 DUPLICATES the tail into
   each arm (address-ordered, 0 goto = the decompiler's target form); -O2 MERGES them (one arm
   jumps into the other's tail = the address-inverted shared tail the gate can't place). */
int family2_common_tail(int a, int b) {
    if (cond(a)) {
        msg(0x2b);
        draw(a);
        redraw(1);        /* COMMON TAIL */
        redraw(2);
        return draw_window();
    }
    if (cond(b)) {
        msg(0x31);
        draw(b);
        redraw(1);        /* COMMON TAIL (identical to above) */
        redraw(2);
        return draw_window();
    }
    return cond(b);
}
