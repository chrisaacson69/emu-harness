"""walk/guard + walk/prep (MIGRATION.md row J)."""
from emu_harness.walk.guard import find_guard_violations, name_map_violations
from emu_harness.walk.prep import frontier, levels, make_clusters


def test_identifier_guard_passes_renames_and_refuses_logic_and_bleed():
    before = {"bank_02.c": "x = sub_8003(a, 1);\n", "bank_01.c": "y = 2;\n"}
    renamed = {"bank_02.c": "x = calc_tax(a, 1);\n", "bank_01.c": "y = 2;\n"}
    assert find_guard_violations(before, renamed, {"bank_02.c"}) == ([], [])
    logic = {"bank_02.c": "x = calc_tax(a, 2);\n", "bank_01.c": "y = 2;\n"}
    assert find_guard_violations(before, logic, {"bank_02.c"}) == ([], ["bank_02.c"])
    bleed = {"bank_02.c": "x = calc_tax(a, 1);\n", "bank_01.c": "y = calc_tax;\n"}
    assert find_guard_violations(before, bleed, {"bank_02.c"})[0] == ["bank_01.c"]


def test_name_map_guard_refuses_a_rename_to_the_wrong_routine():
    before = {"M.c": "f = sub_1000(); g = sub_2000();\n"}
    names = {"open_route": "sub_1000", "close_route": "sub_2000"}
    right = {"M.c": "f = open_route(); g = sub_2000();\n"}
    wrong = {"M.c": "f = close_route(); g = sub_2000();\n"}     # identifier-collapse would pass this
    assert name_map_violations(before, right, names) == []
    assert name_map_violations(before, wrong, names)[0][0] == "M.c"
    assert find_guard_violations(before, wrong, {"M.c"}) == ([], [])


def test_frontier_levels_and_clusters():
    callees = {"a": {"b"}, "b": {"c"}, "c": set(), "d": {"d"}}      # a -> b -> c; d calls itself
    unnamed = {"a", "b", "c", "d"}
    assert frontier(unnamed, callees)[0] == ["c", "d"]
    lv, cyc = levels(unnamed, callees)
    assert (lv["c"], lv["b"], lv["a"], lv["d"]) == (0, 1, 2, 0) and cyc == []
    batch = [("M", i) for i in range(5)] + [("N", 9)]
    cl = make_clusters(batch, lambda r: r[1], 4)
    assert [c["name"] for c in cl] == ["M:1", "mixed:1"] and cl[1]["routines"] == [4, 9]
