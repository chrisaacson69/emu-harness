"""A call graph that orders inference: who calls whom, so a walk names the leaves first (row H of
MIGRATION.md).

Host-neutral. A node is (bank, addr): a routine's entry in the bank that owns it. A builder (the
KOEI VM's in vm/koei/nes/callgraph, a native-6502 one, an SNES one) fills edges with a kind --
"call", "jsr", "jmp", "bankcall", "bankrun", "dispatch" -- and records `roots`: entries reached
from outside any routine (hardware vectors, dispatch tables), so a routine with no caller and no
root is a real orphan, not one reached indirectly.
"""
from __future__ import annotations

from collections import Counter, defaultdict


class Graph:
    def __init__(self, title):
        self.title = title
        self.subs = {}                       # (bank, addr) -> name or None: the routines being walked
        self.edges = defaultdict(set)        # (src, dst) -> {kinds}
        self.bank_edges = defaultdict(set)   # (src_bank, dst_bank) -> {kinds}, incl. edges with a computed entry
        self.roots = defaultdict(set)        # node -> {"vector:NMI", "table:syscall", ...}
        self.data = defaultdict(set)         # src node -> {banks it reads data from}
        self.unresolved = []                 # (src, addr, why): a call whose owner is not known
        self.dynamic = Counter()             # kind -> bank-crossing calls whose bank is not a constant
        self.failed = []                     # routines a builder could not fully read

    def add(self, src, dst, kind):
        self.edges[(src, dst)].add(kind)
        if dst[0] != src[0]:
            self.bank_edges[(src[0], dst[0])].add(kind)

    def callers(self, node):
        """[(src, kinds)] of every edge into `node`, plus its roots as (None, {root})."""
        out = [(s, ks) for (s, d), ks in self.edges.items() if d == node and s != node]
        return out + [(None, {r}) for r in sorted(self.roots.get(node, ()))]

    def callees(self, node):
        return [(d, ks) for (s, d), ks in self.edges.items() if s == node and d != node]

    def orphans(self, bank=None):
        """Routines nothing calls and no root reaches: the walk's unexplained entries."""
        called = {d for (s, d) in self.edges if s != d} | set(self.roots)
        return sorted(n for n in self.subs if n not in called and (bank is None or n[0] == bank))


def leaves_first(G, nodes=None):
    """Routines ordered so each comes after everything it calls (a cycle is broken at the member
    with the fewest pending callees): the bottom-up order a walk names them in."""
    calls = defaultdict(set)
    for (s, d) in G.edges:
        if d in G.subs and d != s:
            calls[s].add(d)
    todo = set(nodes or G.subs)
    order = []
    while todo:
        ready = sorted(n for n in todo if not (calls[n] & todo) - {n})
        if not ready:
            ready = [min(todo, key=lambda n: (len((calls[n] & todo) - {n}), n))]
        for n in ready:
            order.append(n)
            todo.discard(n)
    return order


def bank_order(G):
    """Banks leaves-first: a bank after every bank it calls; cycles broken by fewest open callees."""
    banks = sorted({b for b, _a in G.subs})
    calls = defaultdict(set)
    for (s, d) in G.bank_edges:
        if s in banks and d in banks and s != d:
            calls[s].add(d)
    todo, order = set(banks), []
    while todo:
        ready = sorted(b for b in todo if not calls[b] & todo)
        if not ready:
            ready = [min(todo, key=lambda b: (len(calls[b] & todo), b))]
        order += ready
        todo -= set(ready)
    return order
