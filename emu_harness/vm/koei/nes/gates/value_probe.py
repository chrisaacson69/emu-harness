"""Value probe: the rendered C of a sub, evaluated, against the ROM running the same sub.

The check the structural gates cannot make. round-trip proves the bytes, cfg_gate the control flow,
compare_probe each compare opcode; none of them reads the C the decompiler prints. Four NA1 bugs
lived exactly there with every gate green: compare names swapped (323 sites), the $8B immediate's
dropped sign, a textual inverter that turned `!(A || B)` into `A && !B` ($82E9), and, before
2026-06-03, call arguments printed in push order. This probe prints a sub's structured C the normal
way, evaluates it over an input grid, and requires the value the ROM's own interpreter returns
(titles.run_sub). A call inside the C is evaluated by running the callee on the ROM with the
arguments in the order the C prints them, so a swapped argument list disagrees too.

Converted from two na1-decompiler attic probes (2026-10-27): probe-ternary-folds ran min_word /
max_word / is_tile_in_bounds on the ROM but compared them with hand-written Python rather than with
the C, so a decompiler regression passed it; probe-espionage asked the argument order once, by hand.

The evaluator covers only what the fixtures print: `return E;` and `if (E) return E;` lines over
integer literals, args, `(unsigned)` / `(word)` / `(sword)` casts, + - * << >> & | ^, the six
comparisons, && || ! ?:, and calls. Words are 16-bit; a comparison is signed unless an operand is
cast `(unsigned)`. Anything else raises Unsupported, and the probe fails rather than passing a
fixture it could not read.

Usage:
  py -3.14 -m emu_harness.vm.koei.nes.gates.value_probe [-v]      # NA1's fixtures; exit 1 on a mismatch
"""
import argparse
import contextlib
import io
import itertools
import re
import sys
import tempfile
from pathlib import Path

EDGE = (0, 1, 5, 0x7FFF, 0x8000, 0xFFFF)
XY = (0, 2, 3, 4, 5, 10, 11, 0xFFFF)
BITS = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80)

# (name, bank, stub, argument grid). Each is pure in its arguments, or reads only ROM-initial state
# through a callee the ROM also runs.
FIXTURES = {
    "na1": [
        ("min_word", 15, 0xCB5E, [EDGE, EDGE]),                    # value-diamond polarity, signedness
        ("max_word", 15, 0xCB6F, [EDGE, EDGE]),
        ("is_tile_in_bounds", 2, 0x8F11, [XY, XY]),                # && fold, unsigned bounds
        ("unit_strength_ptr", 2, 0x82C9, [EDGE, EDGE]),            # non-commutative arithmetic
        ("is_cell_clear_of_bits", 2, 0x82DB, [XY, XY, BITS]),      # inverted || ; read_map_cell(x, y) order
    ],
}


class Unsupported(Exception):
    pass


_TOK = re.compile(r"\s*(0x[0-9A-Fa-f]+|\d+|[A-Za-z_]\w*|<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%<>&|^!~?:(),])")
_BINARY = [("||",), ("&&",), ("|",), ("^",), ("&",), ("==", "!="), ("<", "<=", ">", ">="), ("<<", ">>"),
           ("+", "-"), ("*",)]
_CASTS = {"unsigned", "word", "sword"}


def _tokens(s):
    out, i = [], 0
    while i < len(s):
        m = _TOK.match(s, i)
        if not m:
            if s[i:].strip():
                raise Unsupported(f"cannot tokenize {s[i:]!r}")
            break
        out.append(m.group(1)); i = m.end()
    return out


class _Parser:
    """Recursive descent over C precedence -> a closure env -> (value16, unsigned_flag)."""

    def __init__(self, toks, call):
        self.t, self.i, self.call = toks, 0, call

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else None

    def take(self, want=None):
        tok = self.peek()
        if want is not None and tok != want:
            raise Unsupported(f"expected {want!r}, got {tok!r}")
        self.i += 1
        return tok

    def parse(self):
        e = self.ternary()
        if self.peek() is not None:
            raise Unsupported(f"trailing {self.t[self.i:]}")
        return e

    def ternary(self):
        c = self.binary(0)
        if self.peek() != "?":
            return c
        self.take("?"); a = self.ternary(); self.take(":"); b = self.ternary()
        return lambda env: a(env) if _truth(c(env)) else b(env)

    def binary(self, level):
        if level == len(_BINARY):
            return self.unary()
        left = self.binary(level + 1)
        while self.peek() in _BINARY[level]:
            op = self.take()
            right = self.binary(level + 1)
            left = _binop(op, left, right)
        return left

    def unary(self):
        tok = self.peek()
        if tok == "!":
            self.take(); e = self.unary()
            return lambda env: (0 if _truth(e(env)) else 1, False)
        if tok == "-":
            self.take(); e = self.unary()
            return lambda env: ((-e(env)[0]) & 0xFFFF, False)
        if tok == "~":
            self.take(); e = self.unary()
            return lambda env: ((~e(env)[0]) & 0xFFFF, False)
        if tok == "(" and self.peek(1) in _CASTS and self.peek(2) == ")":
            self.take(); kind = self.take(); self.take(")")
            e = self.unary()
            return lambda env: (e(env)[0], kind == "unsigned")
        if tok in ("*", "&"):
            raise Unsupported("memory access")
        return self.primary()

    def primary(self):
        tok = self.take()
        if tok is None:
            raise Unsupported("unexpected end")
        if tok == "(":
            e = self.ternary(); self.take(")")
            return e
        if re.fullmatch(r"0x[0-9A-Fa-f]+|\d+", tok):
            v = int(tok, 0) & 0xFFFF
            return lambda env: (v, False)
        if self.peek() == "(":
            self.take("(")
            args = []
            while self.peek() != ")":
                args.append(self.ternary())
                if self.peek() == ",":
                    self.take(",")
            self.take(")")
            return lambda env, name=tok: (self.call(name, [a(env)[0] for a in args]) & 0xFFFF, False)
        if re.fullmatch(r"arg\d+", tok):
            return lambda env: (env[tok], False)
        raise Unsupported(f"identifier {tok!r} (a global or local)")


def _truth(v):
    return v[0] != 0


def _s(v):
    return v - 0x10000 if v & 0x8000 else v


def _binop(op, l, r):
    if op == "&&":
        return lambda env: (1 if _truth(l(env)) and _truth(r(env)) else 0, False)
    if op == "||":
        return lambda env: (1 if _truth(l(env)) or _truth(r(env)) else 0, False)

    def f(env):
        (a, ua), (b, ub) = l(env), r(env)
        if op in ("<", "<=", ">", ">=", "==", "!="):
            x, y = (a, b) if (ua or ub) else (_s(a), _s(b))
            return (int({"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y, "==": x == y, "!=": x != y}[op]), False)
        v = {"+": a + b, "-": a - b, "*": a * b, "<<": a << (b & 15), ">>": a >> (b & 15),
             "&": a & b, "|": a | b, "^": a ^ b}[op]
        return (v & 0xFFFF, False)
    return f


def compile_sub(c_text, call):
    """The C of one sub -> (name, nargs, fn(args) -> word). Raises Unsupported."""
    lines = [re.sub(r"//.*", "", ln).strip() for ln in c_text.splitlines()]
    lines = [ln for ln in lines if ln]
    head = next((ln for ln in lines if re.match(r"word \w+\(", ln)), None)
    if not head:
        raise Unsupported("no function header")
    m = re.match(r"word (\w+)\((.*)\) \{$", head)
    if not m:
        raise Unsupported(f"header {head!r}")
    nargs = len(re.findall(r"\barg\d+\b", m.group(2)))
    body = lines[lines.index(head) + 1:]
    if not body or body[-1] != "}":
        raise Unsupported("body does not close")
    stmts = []
    for ln in body[:-1]:
        g = re.fullmatch(r"if \((.*)\) return (.*);", ln)
        if g:
            stmts.append((_Parser(_tokens(g.group(1)), call).parse(), _Parser(_tokens(g.group(2)), call).parse()))
            continue
        g = re.fullmatch(r"return (.*);", ln)
        if g:
            stmts.append((None, _Parser(_tokens(g.group(1)), call).parse()))
            continue
        raise Unsupported(f"statement {ln!r}")

    def run(args):
        env = {f"arg{i + 1}": a & 0xFFFF for i, a in enumerate(args)}
        for cond, val in stmts:
            if cond is None or _truth(cond(env)):
                return val(env)[0]
        raise Unsupported("fell off the end")
    return m.group(1), nargs, run


_DEFAULT_FLAGS = {"STRUCTURE": True, "STRUCTURE_V2": False, "STRUCTURE_DREAM": False}


def rendered_c(game, bank, stub):
    """The structured C the decompiler prints for one sub, as `titles decompile` would."""
    from emu_harness.vm.koei.nes import titles
    VC, _cfg, labs, _sys = titles.decompiler_for(game, bank)
    saved = {k: getattr(VC, k) for k in _DEFAULT_FLAGS}
    try:
        for k, v in _DEFAULT_FLAGS.items():
            setattr(VC, k, v)
        with tempfile.TemporaryDirectory() as td:
            asm = Path(td) / "bank.asm"
            with contextlib.redirect_stdout(io.StringIO()):
                titles.disasm(game, bank, str(asm))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                VC.decompile(str(asm), stub, labels=labs)
        return buf.getvalue(), labs
    finally:
        for k, v in saved.items():
            setattr(VC, k, v)


def check(game="na1", verbose=False):
    """-> list of (fixture, problem). Empty = every fixture's C equals the ROM on its grid."""
    from emu_harness.vm.koei.nes.titles import run_sub
    bad = []
    for name, bank, stub, grid in FIXTURES[game]:
        c_text, labs = rendered_c(game, bank, stub)
        by_name = {n: a for a, (n, _c) in labs.items()}

        def call(fn, args, bank=bank):
            m = re.fullmatch(r"sub_([0-9A-Fa-f]{4})", fn)
            addr = int(m.group(1), 16) if m else by_name.get(fn)
            if addr is None:
                raise Unsupported(f"call to unknown {fn}")
            return run_sub(game, addr, args, bank=bank)
        try:
            _n, nargs, fn = compile_sub(c_text, call)
            if nargs != len(grid):
                raise Unsupported(f"{nargs} args, grid has {len(grid)}")
            misses = []
            for args in itertools.product(*grid):
                rom, c = run_sub(game, stub, list(args), bank=bank), fn(args)
                if rom != c:
                    misses.append((args, rom, c))
        except Unsupported as e:
            bad.append((name, f"unsupported: {e}"))
            continue
        if misses:
            a, rom, c = misses[0]
            bad.append((name, f"{len(misses)} input(s) differ, e.g. {tuple(hex(x) for x in a)}: ROM {rom:#06x}, C {c:#06x}"))
        elif verbose:
            print(f"  {name} ${stub:04X}: OK over {len(list(itertools.product(*grid)))} inputs")
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rendered C evaluated against the ROM, by value")
    ap.add_argument("--game", default="na1", choices=sorted(FIXTURES))
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    bad = check(a.game, a.verbose)
    for name, why in bad:
        print(f"  {name}: {why}")
    n = len(FIXTURES[a.game])
    print(f"{a.game}: {n - len(bad)}/{n} fixtures' rendered C equals the ROM")
    print("FAIL" if bad else "PASS")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
