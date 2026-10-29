/* Conditionals: the if-family + the guard / shared-return atoms that drove NA1's
   $A2D2 / $AD38 "cross-edge" residue. One construct per function. */
extern void use(int);
extern int  src(void);

/* plain if: one forward conditional branch skipping the then-block */
int c_if(int a) {
    int r = 0;
    if (a) use(1);
    return r;
}

/* if/else: branch to else-block, then-block jumps over it to the merge */
int c_if_else(int a) {
    int r = 0;
    if (a) r = 1;
    else   r = 2;
    return r;
}

/* else-if chain: a staircase of forward branches to a shared merge */
int c_elseif_chain(int a) {
    int r = 0;
    if (a == 1)      r = 10;
    else if (a == 2) r = 20;
    else if (a == 3) r = 30;
    else             r = 40;
    return r;
}

/* guard / early return: branch-to-terminal. NA1 atom candidate
   "branch-to-terminal => guard statement". */
int c_guard(int a) {
    if (!a) return 0;
    use(a);
    return 1;
}

/* guard chain: N branches to a SHARED return block. This is the exact shape
   top-down called "cross-edge / shared continuation" (NA1 $AD38, $A2D2). */
int c_guard_chain(int a, int b) {
    if (!a) return 0;
    if (!b) return 0;
    if (a < b) use(1);
    else       use(2);
    return 1;
}
