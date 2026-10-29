/* The NA1 atom-4 shape, reproduced forward. The decompiler-atom-log says switches
   fail to fold because the switch exit M = post-dom(S) is a SHARED block — reached
   not only by the cases but ALSO by a pre-switch guard or a shared return. That
   raises M's in-degree past #cases and breaks the "M is single-entry-dominated by
   S" check. Here we manufacture exactly that, to confirm the forward shape. */
extern void use(int);
extern int  src(void);

/* $9C84 shape: an early guard returns to the SAME block the switch cases converge
   on (a shared return). M is reached by {early guard} U {all cases}. */
int sm_guard_shares_merge(int a) {
    int r = 0;
    if (!src()) return r;          /* early-out jumps to the shared return */
    switch (a) {
        case 0: r = 1; break;
        case 1: r = 2; break;
        case 2: r = 3; break;
        default: r = 9; break;
    }
    return r;                       /* M = this return, shared with the guard */
}

/* $9ED9 shape: the switch DEFAULT falls to a block that a PRE-switch block also
   reaches — M sits between case addresses (interleaved) and is not S-dominated. */
int sm_default_preswitch_share(int a, int b) {
    int r = 0;
    if (b) r = 7;                   /* pre-switch block, falls to the merge path */
    switch (a) {
        case 0: r += 1; break;
        case 1: r += 2; break;
        default: break;             /* default => merge, also reached from above */
    }
    use(r);
    return r;
}
