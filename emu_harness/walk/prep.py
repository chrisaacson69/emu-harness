"""Batch selection for a naming walk (row J of MIGRATION.md): which unnamed routines the next wave
names, and how they are grouped for the agents.

Over any call graph -- `callees[r]` = the routines r calls (a passes.callgraph Graph's edges, or a
dict) -- with `unnamed` the routines still to name:

  frontier   unnamed routines none of whose callees is unnamed (self-calls ignored): every routine
             it reads is already named, the bottom-up fixpoint the walks rely on
  levels     every unnamed routine by depth over the unnamed sub-graph, for one multi-level run
             (passes.callgraph.layers; a recursive group shares a level and is reported)
  clusters   per-group chunks keep related routines together; remainders under half a chunk are
             pooled into mixed clusters so a lone leaf does not cost a whole agent

From AeroBiz-decompiler tools/walk_prep.py.
"""
from collections import defaultdict

from emu_harness.passes.callgraph import Graph, layers, sccs


def unnamed_deps(unnamed, callees):
    return {r: {c for c in callees.get(r, ()) if c in unnamed and c != r} for r in unnamed}


def frontier(unnamed, callees):
    """-> (routines, note). With no routine qualifying (a call cycle), the ones with the fewest
    unnamed callees are taken instead, and the note says so."""
    deps = unnamed_deps(unnamed, callees)
    out = sorted(r for r in unnamed if not deps[r])
    if not out and unnamed:
        low = min(len(v) for v in deps.values())
        out = sorted(r for r in unnamed if len(deps[r]) == low)
        return out, "CYCLE BREAK: no routine has all callees named; taking the %d with %d unnamed callee(s)" % (len(out), low)
    return out, "leaves-first: every bytecode callee of each routine is already named"


def levels(unnamed, callees):
    """-> ({routine: level}, [routines in a call cycle]) over the unnamed sub-graph."""
    G = Graph("walk")
    for r in unnamed:
        G.subs[r] = None
    deps = unnamed_deps(unnamed, callees)
    for r, cs in deps.items():
        for c in cs:
            G.add(r, c, "call")
    cyc = [n for comp in sccs(sorted(unnamed), lambda n: sorted(deps[n])) if len(comp) > 1 for n in comp]
    return layers(G), sorted(cyc)


def make_clusters(batch, entry, size, prefix="", group=lambda r: r[0]):
    """Per-group chunks of `size`; chunks under half a cluster pooled into mixed clusters.
    `entry(r)` renders one routine for the agents."""
    by = defaultdict(list)
    for r in batch:
        by[group(r)].append(r)
    clusters, pool = [], []
    for g in sorted(by):
        rs = by[g]
        for i in range(0, len(rs), size):
            chunk = rs[i:i + size]
            if len(chunk) * 2 < size:
                pool.extend(chunk)
            else:
                clusters.append(dict(name="%s%s:%d" % (prefix, g, i // size + 1), modules=[g],
                                     routines=[entry(r) for r in chunk]))
    for i in range(0, len(pool), size):
        chunk = pool[i:i + size]
        clusters.append(dict(name="%smixed:%d" % (prefix, i // size + 1), modules=sorted({group(r) for r in chunk}),
                             routines=[entry(r) for r in chunk]))
    return clusters
