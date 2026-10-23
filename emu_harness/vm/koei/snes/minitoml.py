#!/usr/bin/env python3
"""
Minimal TOML reader for the koei-snes registries (tools/games.toml, data/vm_opcode_names.toml).

`py -3` here is Python 3.9 (no tomllib), and the registries must load identically with or
without tomllib. This reader covers exactly the subset those files use -- nothing more:

    [table] / [table.sub] headers        key = value  (bare or "quoted" keys, dotted keys)
    "basic strings" (\\" \\\\ \\n \\t \\r)  integers (decimal, 0x hex, _ separators), true/false
    [arrays] (may span lines, trailing comma, comments inside)
    { inline = tables } (one line; values may be arrays / nested inline tables)

Anything outside the subset raises TomlError with the line number: a registry that does not
parse is an error, never a silent partial load.

    from emu_harness.vm.koei.snes import minitoml
    data = minitoml.loads(open(path, encoding="utf-8").read())
"""


class TomlError(ValueError):
    pass


def loads(text):
    return _Parser(text).parse()


def load_file(path):
    """tomllib when available (3.11+), else this reader -- same dicts either way."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    try:
        import tomllib
    except ImportError:
        return loads(text)
    return tomllib.loads(text)


_BARE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
_ESC = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r"}


class _Parser:
    def __init__(self, text):
        self.s, self.i, self.n = text, 0, len(text)

    # -- low level -------------------------------------------------------------------
    def err(self, msg):
        raise TomlError("line %d: %s" % (self.s.count("\n", 0, self.i) + 1, msg))

    def peek(self):
        return self.s[self.i] if self.i < self.n else ""

    def ws(self):                                  # blanks inside a line
        while self.i < self.n and self.s[self.i] in " \t":
            self.i += 1

    def ws_nl(self):                               # blanks, newlines and comments
        while self.i < self.n:
            c = self.s[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif c == "#":
                while self.i < self.n and self.s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def eol(self):                                 # rest of the line: blanks + optional comment
        self.ws()
        if self.peek() == "#":
            while self.i < self.n and self.s[self.i] != "\n":
                self.i += 1
        if self.peek() == "\r":
            self.i += 1
        if self.i < self.n:
            if self.s[self.i] != "\n":
                self.err("unexpected text %r after value" % self.s[self.i:self.i + 12])
            self.i += 1

    # -- document --------------------------------------------------------------------
    def parse(self):
        root = {}
        cur = root
        while True:
            self.ws_nl()
            if self.i >= self.n:
                return root
            if self.peek() == "[":
                if self.s.startswith("[[", self.i):
                    self.err("arrays of tables are outside the supported subset")
                self.i += 1
                path = self.key_path("]")
                self.i += 1
                cur = root
                for k in path:
                    nxt = cur.setdefault(k, {})
                    if not isinstance(nxt, dict):
                        self.err("key %r is already a value, not a table" % k)
                    cur = nxt
                self.eol()
                continue
            path = self.key_path("=")
            self.i += 1
            self.ws()
            self.assign(cur, path, self.value())
            self.eol()

    def assign(self, table, path, value):
        for k in path[:-1]:
            table = table.setdefault(k, {})
            if not isinstance(table, dict):
                self.err("dotted key %r crosses a value" % k)
        if path[-1] in table:
            self.err("duplicate key %r" % path[-1])
        table[path[-1]] = value

    def key_path(self, stop):
        parts = []
        while True:
            self.ws()
            parts.append(self.key())
            self.ws()
            c = self.peek()
            if c == ".":
                self.i += 1
                continue
            if c == stop:
                return parts
            self.err("expected %r after key %r" % (stop, parts[-1]))

    def key(self):
        if self.peek() == '"':
            return self.string()
        j = self.i
        while self.i < self.n and self.s[self.i] in _BARE:
            self.i += 1
        if j == self.i:
            self.err("expected a key")
        return self.s[j:self.i]

    # -- values ----------------------------------------------------------------------
    def value(self):
        c = self.peek()
        if c == '"':
            return self.string()
        if c == "[":
            return self.array()
        if c == "{":
            return self.inline_table()
        return self.scalar()

    def string(self):
        if self.s.startswith('"""', self.i):
            self.err("multi-line strings are outside the supported subset")
        self.i += 1
        out = []
        while True:
            c = self.peek()
            if c in ("", "\n"):
                self.err("unterminated string")
            if c == '"':
                self.i += 1
                return "".join(out)
            if c == "\\":
                e = self.s[self.i + 1:self.i + 2]
                if e not in _ESC:
                    self.err("unsupported escape \\%s" % e)
                out.append(_ESC[e])
                self.i += 2
                continue
            out.append(c)
            self.i += 1

    def array(self):
        self.i += 1
        out = []
        while True:
            self.ws_nl()
            if self.peek() == "]":
                self.i += 1
                return out
            if self.i >= self.n:
                self.err("unterminated array")
            out.append(self.value())
            self.ws_nl()
            c = self.peek()
            if c == ",":
                self.i += 1
            elif c == "]":
                self.i += 1
                return out
            else:
                self.err("expected ',' or ']' in array")

    def inline_table(self):
        self.i += 1
        out = {}
        self.ws()
        if self.peek() == "}":
            self.i += 1
            return out
        while True:
            path = self.key_path("=")
            self.i += 1
            self.ws()
            self.assign(out, path, self.value())
            self.ws()
            c = self.peek()
            if c == ",":
                self.i += 1
                self.ws()
                continue
            if c == "}":
                self.i += 1
                return out
            self.err("expected ',' or '}' in inline table")

    def scalar(self):
        j = self.i
        while self.i < self.n and self.s[self.i] not in " \t\r\n,]}#":
            self.i += 1
        tok = self.s[j:self.i]
        if tok == "true":
            return True
        if tok == "false":
            return False
        t = tok.replace("_", "")
        try:
            if t[:2] == "0x":                      # TOML: lowercase prefix only (as tomllib)
                return int(t[2:], 16)
            if t and (t.lstrip("+-").isdigit()):
                return int(t, 10)
        except ValueError:
            pass
        self.err("unsupported value %r" % tok)
