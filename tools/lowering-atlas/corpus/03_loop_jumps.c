/* break / continue / multi-level break: the loop-exit and loop-back edges that
   are NOT the natural fall-out. NA1 atoms 1 (continue=extra back-edge) and
   2 (multi-level break = honest goto). */
extern void use(int);
extern int  src(void);

/* break: an early exit edge to the loop's exit block (not via the test). */
int j_while_break(int n) {
    int s = 0;
    while (n) { if (src()) break; s += n; n--; }
    return s;
}

/* continue: an early back-edge to the loop header (extra latch). NA1 atom 1. */
int j_while_continue(int n) {
    int s = 0;
    while (n) { n--; if (src()) continue; s += n; }
    return s;
}

int j_for_break(int n) {
    int s = 0;
    for (int i = 0; i < n; i++) { if (src()) break; s += i; }
    return s;
}

int j_for_continue(int n) {
    int s = 0;
    for (int i = 0; i < n; i++) { if (src()) continue; s += i; }
    return s;
}

/* nested loops + break from the inner: single-level break (inner exit). */
int j_nested_break_inner(int n) {
    int s = 0;
    for (int i = 0; i < n; i++)
        for (int k = 0; k < n; k++) { if (src()) break; s += k; }
    return s;
}

/* multi-level escape: a goto out of BOTH loops to a shared target. C has no
   2-level break; the source must use goto. NA1 atom 2. */
int j_multi_level_break(int n) {
    int s = 0;
    for (int i = 0; i < n; i++)
        for (int k = 0; k < n; k++) { if (src()) goto done; s += k; }
done:
    use(s);
    return s;
}
