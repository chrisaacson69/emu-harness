/* Loops: while / do-while / for. The back-edge + header/latch signatures. */
extern void use(int);
extern int  src(void);

/* while: pre-test. GCC -O0 lowers as jump-FORWARD-to-test, body is the latch. */
int l_while(int n) {
    int s = 0;
    while (n) { s += n; n--; }
    return s;
}

/* do-while: post-test. body falls into the latch test, back-edge from the test. */
int l_do_while(int n) {
    int s = 0;
    do { s += n; n--; } while (n);
    return s;
}

/* for: init + pre-test + body + update-latch. */
int l_for(int n) {
    int s = 0;
    for (int i = 0; i < n; i++) s += i;
    return s;
}

/* for with empty sections (for(;;)-style infinite with internal exit) */
int l_for_infinite(int n) {
    int s = 0;
    for (;;) { if (s > n) break; s += src(); }
    return s;
}
