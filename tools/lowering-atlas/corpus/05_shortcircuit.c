/* Short-circuit && / || and ?:  — these lower to branch chains / value-diamonds,
   NOT single tests. NA1's V2 already pre-contracts "value-diamonds" and
   "boolean-regions" (locked decisions #3/#4); this is the forward shape they invert. */
extern void use(int);
extern int  src(void);

/* && : if (a && b) — short-circuit, two chained tests guarding one then-block. */
int b_and(int a, int b) {
    int r = 0;
    if (a && b) r = 1;
    return r;
}

/* || : if (a || b) — first test SHORT-CIRCUITS into the then-block. */
int b_or(int a, int b) {
    int r = 0;
    if (a || b) r = 1;
    return r;
}

/* mixed: (a && b) || c — the full boolean-region shape. */
int b_mixed(int a, int b, int c) {
    int r = 0;
    if ((a && b) || c) r = 1;
    return r;
}

/* ternary: a value-diamond feeding one assignment (no statement-level branch). */
int b_ternary(int a) {
    int r = a ? src() : 0;
    return r;
}

/* && used for value, not control (the diamond as an rvalue). */
int b_and_value(int a, int b) {
    int r = (a && b);
    return r;
}
