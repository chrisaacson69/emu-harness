"""passes/callgraph + the KOEI NES builder (MIGRATION.md row H)."""
import pytest

from emu_harness.passes.callgraph import Graph, bank_order, leaves_first


def test_leaves_first_orders_callees_before_callers_and_breaks_cycles():
    G = Graph("t")
    for n in [(0, 1), (0, 2), (1, 3), (1, 4), (1, 5)]:
        G.subs[n] = None
    G.add((0, 1), (0, 2), "call")      # 1 -> 2
    G.add((0, 2), (1, 3), "call")      # 2 -> 3 (cross-bank)
    G.add((1, 4), (1, 5), "call")      # 4 <-> 5: a cycle
    G.add((1, 5), (1, 4), "call")
    order = leaves_first(G)
    assert order.index((1, 3)) < order.index((0, 2)) < order.index((0, 1))
    assert {(1, 4), (1, 5)} <= set(order) and len(order) == 5
    assert bank_order(G) == [1, 0]
    assert G.orphans() == [(0, 1)]                       # nothing calls 1, and it is not a root
    G.roots[(0, 1)].add("vector")
    assert G.orphans() == [] and (None, {"vector"}) in G.callers((0, 1))


def _built(title):
    from emu_harness.vm.koei.nes import callgraph, titles
    if not titles.game_cfg(title)["_rom"].exists():
        pytest.skip(f"{title} ROM not present")
    return callgraph.build(title)


def test_bk_call_abs_imm1_edges_are_in_the_graph():
    """bk bank 2 calls rand_mod ($E216, fixed bank 31) with CALL_abs_imm1: the form the old regex
    `CALL[_a-z]*\\s+\\$` could not match."""
    G = _built("bk")
    assert ((2, 0xA003), (31, 0xE216)) in G.edges


def test_uw_trampoline_edges_cross_subsystems():
    """UW bank 18 calls bank 29 through call_in_bank_dcd1(1, 221, 0x872B): 221 & 31 = 29. The old
    graph counted direct CALL_abs only, and so reported strict layering."""
    G = _built("uw")
    assert "bankcall" in G.edges[((18, 0x83BB), (29, 0x872B))]


def _tarjan_recursive(nodes, succ):
    """Mappy's original recursive Tarjan (game-annotation mappy/call_graph.py), the reference."""
    import sys
    sys.setrecursionlimit(10000)
    index, low, onst, stack, out, ctr = {}, {}, {}, [], [], [0]

    def strongconnect(v):
        index[v] = low[v] = ctr[0]; ctr[0] += 1; stack.append(v); onst[v] = True
        for w in succ(v):
            if w not in index:
                strongconnect(w); low[v] = min(low[v], low[w])
            elif onst.get(w):
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop(); onst[w] = False; comp.append(w)
                if w == v:
                    break
            out.append(comp)
    for v in nodes:
        if v not in index:
            strongconnect(v)
    return out


def test_iterative_tarjan_matches_the_recursive_order():
    import random
    from emu_harness.passes.callgraph import sccs
    rng = random.Random(7)
    for trial in range(200):
        n = rng.randint(1, 40)
        adj = {v: [rng.randrange(n) for _ in range(rng.randint(0, 4))] for v in range(n)}
        nodes = list(range(n)); rng.shuffle(nodes)
        assert sccs(nodes, lambda v: adj[v]) == _tarjan_recursive(nodes, lambda v: adj[v]), trial


def test_layers_collapse_recursion_and_sites_count_uses():
    from emu_harness.passes.callgraph import layers
    G = Graph("t")
    for n in [(0, 1), (0, 2), (0, 3), (0, 4)]:
        G.subs[n] = None
    G.add((0, 1), (0, 2), "call")
    G.add((0, 1), (0, 2), "call")          # a second call site, same edge
    G.add((0, 2), (0, 3), "call")
    G.add((0, 3), (0, 4), "call")
    G.add((0, 4), (0, 3), "call")          # 3 <-> 4: one layer
    lay = layers(G)
    assert lay[(0, 3)] == lay[(0, 4)] == 0 and lay[(0, 2)] == 1 and lay[(0, 1)] == 2
    assert G.sites[((0, 1), (0, 2))] == 2 and len(G.edges) == 4
