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
        """[(dst, kinds)] of every edge out of `node`, a call to itself included (it is a call)."""
        return [(d, ks) for (s, d), ks in self.edges.items() if s == node]

    def frontier(self, named):
        """Unnamed routines whose every callee that is a routine here is already named (a call to
        anything else -- a native stub, another module, itself unnamed -- does not count as named
        unless it is not a routine of this graph): the routines a bottom-up walk names next."""
        return sorted(n for n in self.subs if not named(n)
                      and all(d not in self.subs or named(d) for d, _ks in self.callees(n)))

    def tops(self):
        """Routines no routine of this graph calls: the roots of the top-down forest."""
        called = {d for (s, d) in self.edges if s in self.subs and d in self.subs}
        return sorted(n for n in self.subs if n not in called)

    def orphans(self, bank=None):
        """Routines nothing calls and no root reaches: the walk's unexplained entries."""
        called = {d for (s, d) in self.edges if s != d} | set(self.roots)
        return sorted(n for n in self.subs if n not in called and (bank is None or n[0] == bank))


def sccs(nodes, succ):
    """Tarjan's strongly connected components, in the order Tarjan emits them: reverse topological,
    so every component comes after the components it calls -- leaves first, a recursive cluster
    kept together. From game-annotation mappy/call_graph.py (its bottom-up labeling order), made
    iterative with the recursive version's exact visiting order. `succ(v)` -> v's callees."""
    index, low, onst, stack, out, ctr = {}, {}, set(), [], [], 0
    for v0 in nodes:
        if v0 in index:
            continue
        index[v0] = low[v0] = ctr; ctr += 1
        stack.append(v0); onst.add(v0)
        work = [(v0, iter(succ(v0)))]
        while work:
            v, it = work[-1]
            for w in it:
                if w not in index:
                    index[w] = low[w] = ctr; ctr += 1
                    stack.append(w); onst.add(w)
                    work.append((w, iter(succ(w))))
                    break
                if w in onst:
                    low[v] = min(low[v], index[w])
            else:
                work.pop()
                if low[v] == index[v]:
                    comp = []
                    while True:
                        w = stack.pop(); onst.discard(w); comp.append(w)
                        if w == v:
                            break
                    out.append(comp)
                if work:
                    u = work[-1][0]
                    low[u] = min(low[u], low[v])
    return out


def postorder(root, succ, member):
    """Leaves-first DFS post-order of one subtree: a node after its whole subtree, callees in sorted
    order, each node once; only `member` nodes are followed. From snes-decompiler
    native_callgraph.dfs_postorder (made iterative, same order)."""
    order, seen = [], {root}
    work = [(root, iter(sorted(succ(root))))]
    while work:
        v, it = work[-1]
        for c in it:
            if c not in seen and member(c):
                seen.add(c)
                work.append((c, iter(sorted(succ(c)))))
                break
        else:
            work.pop()
            order.append(v)
    return order


def leaves_first(G, nodes=None):
    """Routines bottom-up: each after everything it calls, a recursive cluster together (the SCC
    order). The order a walk names them in."""
    calls = defaultdict(list)
    for (s, d) in G.edges:
        if d in G.subs and d != s:
            calls[s].append(d)
    keep = set(nodes or G.subs)
    return [n for comp in sccs(sorted(keep), lambda n: sorted(c for c in calls[n] if c in keep)) for n in comp]


def bank_order(G):
    """Banks (or modules) bottom-up: a bank after every bank it calls, a cycle of banks together."""
    banks = sorted({b for b, _a in G.subs}, key=str)
    calls = defaultdict(set)
    for (s, d) in G.bank_edges:
        if s in banks and d in banks and s != d:
            calls[s].add(d)
    return [b for comp in sccs(banks, lambda b: sorted(calls[b], key=str)) for b in comp]
