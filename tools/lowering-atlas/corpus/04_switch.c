/* switch: the single biggest NA1 lever (365 gotos / 36 subs). NA1's VM has
   first-class switch opcodes ($D5 contiguous / $D9 noncontiguous); GCC chooses
   jump-table (dense) vs if-chain (sparse). Cases fall through to a SHARED merge
   unless broken — the shared-merge M=post-dom(S) is the whole atom-4 problem. */
extern void use(int);
extern int  src(void);

/* dense contiguous keys + break each case: jump-table, all cases -> shared merge. */
int s_dense(int a) {
    int r = 0;
    switch (a) {
        case 0: r = 100; break;
        case 1: r = 101; break;
        case 2: r = 102; break;
        case 3: r = 103; break;
        default: r = 999; break;
    }
    return r;
}

/* sparse keys: GCC lowers to an if/compare chain (or clustered tables). */
int s_sparse(int a) {
    int r = 0;
    switch (a) {
        case 1:    r = 1;  break;
        case 100:  r = 2;  break;
        case 5000: r = 3;  break;
        default:   r = 0;  break;
    }
    return r;
}

/* fall-through: cases 0 and 1 share the case-2 body (C-style fall). The shape
   NA1's $D5 cases use (the disasm confirms cases fall through). */
int s_fallthrough(int a) {
    int r = 0;
    switch (a) {
        case 0:
        case 1:
        case 2: r += src(); /* fall */
        case 3: r += src(); break;
        default: r = -1;
    }
    return r;
}

/* no break anywhere = every case inlines into the next, one straight-line run
   entered at different points. The "switch-case-inline" atom candidate. */
int s_case_inline(int a) {
    int r = 0;
    switch (a) {
        case 0: r += 1;
        case 1: r += 2;
        case 2: r += 4;
        default: r += 8;
    }
    return r;
}

/* default in the MIDDLE of the case list (source order) — tests how the
   default edge is placed vs the case targets (NA1 $D5 stores default FIRST). */
int s_default_middle(int a) {
    int r = 0;
    switch (a) {
        case 0: r = 1; break;
        default: r = 9; break;
        case 2: r = 2; break;
    }
    return r;
}
