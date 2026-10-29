"""The KOEI VM package (vm/koei) as moved in row F: the old-name aliases and per-title data selection.

The fold's oracle is the before/after run of every title's decompile (MIGRATION.md row F); these pin
what that run cannot show on its own."""
import re
import subprocess
import sys

from emu_harness.core.alias import alias_package


def test_alias_gives_the_same_module_objects():
    alias_package("koei_alias_test", "emu_harness.vm.koei", also=("emu_harness.vm.koei.nes",))
    import emu_harness.vm.koei.nes.vm_decompile as real
    import koei_alias_test.vm_decompile as via_alias
    from koei_alias_test import vm_cfg
    assert via_alias is real
    assert vm_cfg.__name__ == "emu_harness.vm.koei.nes.vm_cfg"


def test_python_dash_m_through_an_alias(tmp_path):
    for pkg in ("oldname", "probe_pkg"):
        (tmp_path / pkg).mkdir()
    (tmp_path / "probe_pkg" / "__init__.py").write_text("")
    (tmp_path / "probe_pkg" / "probe.py").write_text(
        "print('ran as', __name__, 'from', __file__.replace('\\\\', '/').split('/')[-2:])\n")
    (tmp_path / "oldname" / "__init__.py").write_text(
        "from emu_harness.core.alias import alias_package\nalias_package(__name__, 'probe_pkg')\n")
    r = subprocess.run([sys.executable, "-m", "oldname.probe"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "ran as __main__ from ['probe_pkg', 'probe.py']" in r.stdout      # the real file, as __main__


class _Bytes:
    def __init__(self, *b):
        self.b = bytes(b) + bytes(16)

    def read(self, a):
        return self.b[a]


def test_lengths_measured_on_the_nes_handlers():
    """F2: what every NES title's own handlers consume (tools/koei_isa_probe.py)."""
    from emu_harness.vm.koei.nes.vm_disasm import instr_len
    assert instr_len(_Bytes(0xAD), 0) == 3                       # blkcopy: 2 operand bytes
    assert [instr_len(_Bytes(op), 0) for op in (0xE0, 0xE1, 0xE2)] == [3, 3, 3]   # bitfield #i16
    assert [instr_len(_Bytes(0xB7, e), 0) for e in (0x0B, 0x0C, 0x13, 0x14)] == [2, 4, 4, 2]
    assert instr_len(_Bytes(0xB7, 0x18), 0) == 6                 # 4-byte immediate


def test_nes_decoder_agrees_with_the_snes_table():
    import importlib.util
    from pathlib import Path
    tool = Path(__file__).resolve().parents[1] / "tools" / "koei_isa_census.py"
    spec = importlib.util.spec_from_file_location("census", tool)
    census = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(census)
    T = census.vm_isa.TABLE
    for op, s in T.primary.items():
        _, cf, size = census.nes(op)
        assert (cf, size) == (s.cf, "var" if s.size is None else s.size), f"${op:02X}"
    for e, s in T.extended.items():
        assert census.nes_ext(e) == s.size, f"$B7 ${e:02X}"


def test_the_fixed_region_is_the_last_16k_of_prg():
    """UW's 512K cart: $C000 is the last 16K of PRG (8K banks 62/63), not 16K bank 15 (F2)."""
    from emu_harness.vm.koei.nes.cpu6502 import Memory
    prg = bytearray(0x80000)
    prg[0x7C000], prg[0x3C000] = 0xAA, 0x15
    m = Memory(b"NES\x1a" + bytes([0x20, 0, 0x52, 0]) + bytes(8) + bytes(prg))
    assert m.read(0xC000) == 0xAA


def test_na1_certified_folds_apply_to_na1_only():
    from emu_harness.vm.koei.nes import vm_decompile as VC
    VC.set_schema("gk")
    assert VC._EXTOP_OVERRIDE == {}
    VC.set_schema("na1")
    assert 0x8327 in VC._EXTOP_OVERRIDE



def test_dream_switch_merging_into_its_loop_header():
    """rot3k2-SNES root $34F4: a switch whose merge is its enclosing loop's header (the merge
    dominates the switch). Excluding the merge's dominance emptied the switch region, orphaned the
    case bodies, and a sink became a loop 'header' -- DREAM crashed emitting it (row G). Needs the
    sibling rot3k2-snes-decompiler checkout and its (gitignored) ROM."""
    import argparse
    from pathlib import Path
    import pytest
    T = Path(__file__).resolve().parents[2] / "rot3k2-snes-decompiler"
    sfc = T / "Romance of the Three Kingdoms II (USA).sfc"
    if not sfc.exists():
        pytest.skip("rot3k2-snes-decompiler ROM not present")
    from emu_harness.machines.snes.mapper import strip_copier_header
    from emu_harness.vm.koei.snes import profile, vm_isa, vm_lift, vm_walk, vm_dream
    from emu_harness.vm.koei.snes.symbols import Symbols
    rom, _ = strip_copier_header(sfc.read_bytes())
    P = profile.select("rot3k2", rom)
    space = vm_walk.Space(rom, P, vm_walk.module_from_args(
        rom, P, argparse.Namespace(module="root", start=None, base=None)))
    isa = vm_isa.bind(rom, P)
    W = vm_walk.walk_module(isa, space)
    R = vm_lift.Lifter(isa, space, Symbols(str(T / "data" / "root"), profile=P), W).lift(0x34F4, W.walks[0x34F4])
    _lines, how = vm_dream.structure(R)
    # Built, and equivalent by the gate; only the duplication cap keeps it out of the C. Before
    # the fix: a TypeError in emission (or, with just the sink guard, 'out of scope').
    assert how in ("dream", "goto: DREAM over-dup")


def test_match_tokens_come_from_bytes_not_names():
    """vm/koei/match: ROM operands and control targets relocate ('A'); RAM operands, immediates and
    frame offsets stay; switch tables keep their case values; ext ops carry their ext byte."""
    from emu_harness.vm.koei.match import token
    assert token([0x0C]) == "0C"                                   # nibble op: operand in the opcode
    assert token([0xD6, 0x17, 0x80]) == token([0xD6, 0x34, 0x92]) == "D6 A"   # a jump, anywhere
    assert token([0xA4, 0x01, 0x71]) == "A4 $7101"                 # a RAM global stays
    assert token([0xA4, 0x00, 0x91]) == "A4 A"                     # a ROM table relocates
    sw = [0xD9, 2, 0, 5, 0, 0x10, 0x80, 9, 0, 0x20, 0x80, 0x30, 0x80]   # n=2: (5 ->), (9 ->), default
    assert token(sw) == "D9 2 5 A 9 A A"
    assert token([0xB7, 0x18, 1, 2, 3, 4]).startswith("B7.18 ")


def test_vocabulary_is_well_formed():
    """vocabulary.toml: every routine has an anchor `title [bN] $ADDR` of a known title, a name, and
    no name or anchor twice (the ROM-backed check -- each anchor is a real sub -- runs in `family`)."""
    import tomllib
    from emu_harness.vm.koei.nes import match, titles
    voc = tomllib.loads(match.VOCABULARY.read_text(encoding="utf-8"))
    known = set(titles.load_games())
    anchors, names = [], []
    for r in voc["routine"]:
        m = match._ANCHOR.match(r["anchor"])
        assert m and m.group(1) in known, r["anchor"]
        assert re.fullmatch(r"[a-z][a-z0-9_]*", r["name"]), r["name"]
        anchors.append(r["anchor"]); names.append(r["name"])
    assert len(set(anchors)) == len(anchors) and len(set(names)) == len(names)
    assert all(c.get("members") and c.get("why") for c in voc.get("conflict", []))


def test_dataxref_kinds_come_from_the_lift_templates():
    """vm/koei/nes/dataxref (row K): $A7 ldC.b is a byte READ (na1's table called it address-of), $A8 /
    $A9 are stores, the $8A/$8C/$8E i16 moves are pointers, and an i16 arithmetic constant is not."""
    from emu_harness.vm.koei.isa import TABLE
    from emu_harness.vm.koei.nes.dataxref import kind_of
    k = lambda code: kind_of(TABLE.op(code))
    assert [k(c) for c in (0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xAB)] == \
        ["read", "read", "read", "read", "write", "write", "read", "read"]
    assert [k(c) for c in (0x8A, 0x8C, 0x8E)] == ["ptr"] * 3
    assert k(0x90) is None and k(0xAF) is None and k(0xAC) is None     # addA #i16, sp_adj, a call


def test_record_field_maps_come_from_the_profiles():
    """vm_decompile's per-title field maps live in games.toml [<title>.fields] (row M)."""
    import emu_harness.vm.koei.nes.vm_decompile as VC
    VC.set_schema("gemfire")
    assert (VC.CPP_VAR, VC.CPP_FIELDS, VC.ARG_FIELDS) == ("", {2: "gold", 4: "food", 6: "soldiers", 14: "owner"},
                                                          {2: "gold", 4: "food", 6: "soldiers", 14: "owner"})
    VC.set_schema("na2")
    assert VC.CPP_VAR == "current_province_ptr" and VC.ARG_FIELDS == {20: "liege", 43: "debt"}
    VC.set_schema("na1")
    assert VC.CPP_FIELDS[24] == "stat_cap" and VC.ARG_FIELDS is VC.CPP_FIELDS


def test_na1_assets_match_the_live_ppu_capture():
    """Row O2: the strategic map's terrain CHR starts at tile 81 ($83BC), not 91 -- tiles 81-90 are the
    live map screen's PT1 (na1 traces/17fiefmapAPPU.dmp) and the tilemaps use them; and a composite face
    is laid out by its bank-9 tile-map slices, not in upload order. Needs the sibling na1 checkout + ROM."""
    import pytest
    from emu_harness.vm.koei.nes.titles import game_cfg
    from emu_harness.vm.koei.nes.assets import portrait, strategic_map as sm
    cfg = game_cfg("na1")
    cap = cfg["_root"] / "traces" / "17fiefmapAPPU.dmp"
    if not (cfg["_rom"].exists() and cap.exists()):
        pytest.skip("na1-decompiler ROM / PPU capture not present")
    prg = sm._prg(cfg)
    chr, pt1 = sm.assemble_chr(prg), cap.read_bytes()[0x1000:0x2000]
    used = set().union(*(sm.section_tilemap(prg, s) for s in range(sm.NA1["n_sec"])))
    assert {81, 87, 90} <= used
    assert all(chr[v * 16: v * 16 + 16] == pt1[v * 16: v * 16 + 16] for v in used)
    cells = portrait.composite_layout(prg, (1, 2, 3, 4))
    assert len(cells) == 36 and cells != bytes(range(0x5B, 0x5B + 36))


def test_na1_animation_matches_mesen():
    """Row O3: Mesen's PPU during Train (anim 10) holds sprite palette $3F10-$3F1F below, backdrop $3E
    (the $3F10 mirror), and every unused OAM slot at Y=$F8. The headless VM must reproduce the palette
    and leave no phantom sprite at (0,0). Needs the sibling na1 checkout + ROM."""
    import pytest
    from emu_harness.vm.koei.nes.titles import game_cfg
    from emu_harness.vm.koei.nes.assets import animation as A
    if not game_cfg("na1")["_rom"].exists():
        pytest.skip("na1-decompiler ROM not present")
    frames, _ = A.interpret_animation(10)
    assert len(frames) > 1
    for oam, pal, _chr in frames:
        assert pal[0x10:] == bytes.fromhex("3e1638303e1222303e2722383e162930")
        assert all(oam[i * 4] >= 0xEF or oam[i * 4: i * 4 + 4] != b"\0\0\0\0" for i in range(64))


def test_compare_probe_measures_direction_and_fails_on_a_swap():
    """The compare gate (converted from na1's atticked probe, 2026-10-27): the decompiler's map
    agrees with NA1's live handlers, and the June 2026 bug (C3/C5 le<->ge swapped) is caught."""
    import pytest
    from emu_harness.vm.koei.nes.titles import game_cfg
    from emu_harness.vm.koei.nes import vm_decompile
    from emu_harness.vm.koei.nes.gates import compare_probe
    if not game_cfg("na1")["_rom"].exists():
        pytest.skip("na1-decompiler ROM not present")
    rows = compare_probe.check("na1")
    assert [p for _, p, ok, _ in rows] == list(compare_probe.PREDS) and all(ok for *_, ok, _ in rows)
    swapped = dict(vm_decompile.OPCODE_TO_MNEMONIC)
    swapped[0xC3], swapped[0xC5] = swapped[0xC5], swapped[0xC3]
    bad = [op for op, _, ok, _ in compare_probe.check("na1", dec_map=swapped) if not ok]
    assert bad == [0xC3, 0xC5]


def test_ext_compare_names_measured_and_a_swapped_name_fails():
    """The 32-bit half of the compare gate: NA1's 10 cmp32 names hold on the live handlers, and a
    gt handler carrying the lt name (how na2 / bk / GemFire inherited them) is caught."""
    import pytest
    from emu_harness.core.symbols import names_by_address, read_registry
    from emu_harness.vm.koei.nes.titles import game_cfg
    from emu_harness.vm.koei.nes.gates import compare_probe
    cfg = game_cfg("na1")
    if not cfg["_rom"].exists():
        pytest.skip("na1-decompiler ROM not present")
    rows = compare_probe.check_ext("na1")
    assert len(rows) == 10 and all(r[-1] for r in rows)
    names = names_by_address(read_registry(cfg["_labels"]))
    names[0xF47E] = "cmp32_a_lt_b_signed_f47e"          # the gt handler, given lt's name
    bad = [r for r in compare_probe.check_ext("na1", names=names) if not r[-1]]
    assert [(r[0], r[4]) for r in bad] == [(0x0A, "sgt")]


def test_ext_op_overrides_match_the_rom_and_a_wrong_model_fails():
    """NA1's five formula overrides equal the ROM's own subs over the edge grid (cfg_gate's value
    witness), and the June math32_3arg (c == 0 -> 0, unsigned) is caught."""
    import pytest
    from emu_harness.vm.koei.nes.titles import game_cfg
    from emu_harness.vm.koei.nes import vm_decompile as VC
    from emu_harness.vm.koei.nes.gates import cfg_gate
    if not game_cfg("na1")["_rom"].exists():
        pytest.skip("na1-decompiler ROM not present")
    VC.set_schema("na1")
    small = (0, 1, 7, 10, 0x7FFF, 0x8000, 0xFFFF)
    for stub in VC._EXTOP_OVERRIDE:
        assert cfg_gate.override_value_check(stub, small) == [], hex(stub)
    c_lines, _model, bank = VC._EXTOP_OVERRIDE[0xD6B8]
    VC._EXTOP_OVERRIDE[0xD6B8] = (c_lines, lambda a, b, c: (a * b) // c if c else 0, bank)
    try:
        assert cfg_gate.override_value_check(0xD6B8, small)
    finally:
        VC._EXTOP_OVERRIDE[0xD6B8] = (c_lines, _model, bank)


def test_byte_immediate_keeps_its_sign(tmp_path):
    """$8B BYTE_LOADR_imm1 sign-extends (the live handler: FF -> $FFFF). The decompiler dropped the
    sign, so NA1's arms purchase ($B4D5, the $B5BD test of scale_div10_capcheck's -1 sentinel)
    printed `!= 1`. Disassemble and decompile bank 1 from the ROM and require `!= -1`."""
    import pytest
    from emu_harness.vm.koei.nes import titles
    if not titles.game_cfg("na1")["_rom"].exists():
        pytest.skip("na1-decompiler ROM not present")
    asm, c = tmp_path / "b1.asm", tmp_path / "b1.c"
    titles.disasm("na1", 1, str(asm))
    titles.decompile("na1", 1, str(asm), str(c))
    text = c.read_text(encoding="utf-8")
    sub = text[text.index("sub $B4D5\n"):]
    sub = sub[:sub.index("\n}\n")]
    import re   # the $B5BD compare: its own line (goto form) or folded into a compound condition (DREAM)
    assert re.search(r"local8 [!=]= -1\)", sub) and not re.search(r"local8 [!=]= 1\)", sub), sub


def _eval_c(cond, env):
    """Evaluate a decompiled C condition over small ints (casts dropped; enough for the inverter)."""
    py = re.sub(r"\(unsigned\)", "", cond).replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    return bool(eval(py, {}, dict(env)))


def test_invert_condition_negates_at_the_top_level():
    """invert_condition must be the logical negation. The textual version split at the first
    comparison anywhere, so NA1's `!((A > 10) || (B > 4))` (is_cell_clear_of_bits $82E9, and
    $89FD, $930A) inverted to `!((A <= 10) || (B > 4))`, i.e. A && !B; the ROM returns 1 for
    (0, 5). Each case is checked by value, and the textual inverter is planted and must fail."""
    import itertools
    from emu_harness.vm.koei.nes.vm_decompile import invert_condition

    def textual(cond):   # the pre-fix inverter, kept as the planted fault
        cond = cond.strip()
        if cond.startswith('(') and cond.endswith(')'):
            cond = cond[1:-1]
        for op, inv in (('<', '>='), ('>', '<='), ('<=', '>'), ('>=', '<'), ('==', '!='), ('!=', '==')):
            m = re.match(rf'^(.+?)\s+{re.escape(op)}\s+(.+?)$', cond)
            if m:
                return f"({m.group(1)} {inv} {m.group(2)})"
        return f"!({cond})"

    cases = ["!((((unsigned)a > (unsigned)10) || ((unsigned)b > (unsigned)4)))",
             "!((a || (b <= 0)))", "(a == 1) && (b == 2)", "((a + b) == c)", "a < b",
             "!((a == b))", "(a & b) == c", "a == b & c", "!(a) && !(b)", "((a - b) > 5)"]
    grid = [dict(zip("abc", v)) for v in itertools.product((0, 1, 2, 5, 11), repeat=3)]
    for cond in cases:
        inv = invert_condition(cond)
        assert all(_eval_c(inv, e) != _eval_c(cond, e) for e in grid), (cond, inv)
    assert invert_condition("!((a == b))") == "(a == b)"
    assert any(_eval_c(textual(c), e) == _eval_c(c, e) for c in cases for e in grid)


def test_value_probe_passes_and_planted_renderings_fail():
    """gates.value_probe evaluates the rendered C against the ROM. It must pass on NA1, and fail when
    the decompiler is given back two bugs it had: the textual inverter (2026-10-27, $82E9's
    `!(A || B)` printed as `A && !B`) and call arguments in push order (fixed 2026-06-03, M3)."""
    import pytest
    from emu_harness.vm.koei.nes import titles, vm_decompile as VC
    from emu_harness.vm.koei.nes.gates import value_probe as VP
    if not titles.game_cfg("na1")["_rom"].exists():
        pytest.skip("na1-decompiler ROM not present")
    assert VP.check() == []

    def textual(cond):
        cond = cond.strip()
        if cond.startswith('(') and cond.endswith(')'):
            cond = cond[1:-1]
        for op, inv in (('<', '>='), ('>', '<='), ('<=', '>'), ('>=', '<'), ('==', '!='), ('!=', '==')):
            m = re.match(rf'^(.+?)\s+{re.escape(op)}\s+(.+?)$', cond)
            if m:
                return f"({m.group(1)} {inv} {m.group(2)})"
        return f"!({cond})"

    inv, enums = VC.invert_condition, VC._apply_arg_enums
    try:
        VC.invert_condition = textual
        assert [n for n, _ in VP.check()] == ["is_cell_clear_of_bits"]
        VC.invert_condition = inv
        VC._apply_arg_enums = lambda f, a: enums(f, a)[::-1]
        assert [n for n, _ in VP.check()] == ["is_cell_clear_of_bits"]
    finally:
        VC.invert_condition, VC._apply_arg_enums = inv, enums


def test_value_probe_refuses_what_it_cannot_read():
    """An unsupported construct is a failure, never a silent pass."""
    import pytest
    from emu_harness.vm.koei.nes.gates.value_probe import compile_sub, Unsupported
    with pytest.raises(Unsupported):
        compile_sub("word f(word arg1) {\n    return *(byte*)(arg1);\n}\n", lambda n, a: 0)
    with pytest.raises(Unsupported):
        compile_sub("word f(word arg1) {\n    local9 = arg1;\n    return local9;\n}\n", lambda n, a: 0)
    _n, k, fn = compile_sub("word f(word arg1, word arg2) {\n    return ((arg1 < arg2) ? arg1 : arg2);\n}\n", None)
    assert k == 2 and fn((0xFFFF, 1)) == 0xFFFF and fn((5, 3)) == 3      # signed unless cast


def test_record_access_renders_c_struct_arrays_and_only_that_shape():
    """A strided record table renders as a C array of structs (the profile's [fields] type / base /
    stride). Only `(E * stride) + base..base+stride-1` matches: a number is never renamed, an offset with
    no field name is left alone, and a local is a record pointer only if it is assigned once."""
    from emu_harness.vm.koei.nes import vm_decompile as VC
    rec = {"type": "province", "base": 0x7001, "stride": 26, "fields": {0: "gold", 8: "output", 16: "men", 18: "morale"},
           "width": "word", "returned_by": []}
    labs = {0x7001: ("province_table_live", "")}
    A = lambda t: VC.annotate_record_access(t, labs, rec)
    assert A("x = *(word*)(((i * 26) + 0x7013));") == "x = province_table_live[i].morale;"
    assert A("f(((sel(k) * 26) + 0x7011));") == "f(&province_table_live[sel(k)].men);"
    assert A("p = ((i * 26) + 0x7001);") == "p = &province_table_live[i];"
    assert A("q = (((i * 26) + 0x7001) + 8);") == "q = &province_table_live[i].output;"
    assert A("b = *(byte*)(((i * 26) + 0x7013));") == "b = *(byte*)(&province_table_live[i].morale);"
    for keep in ("n = 0x7530;", "n = ((i * 26) + 0x7003);", "n = ((i * 7) + 0x7013);", "n = ((i * 26) + 0x7101);"):
        assert A(keep) == keep, keep
    L = lambda ts: [t for _a, _i, t in VC.annotate_record_locals([(0, 1, t) for t in ts], rec)]
    assert L(["local5 = &province_table_live[i];", "x = *(word*)((local5 + 16));", "y = *(word*)(local5);",
              "z = (local5 + 18);"]) == ["local5 = &province_table_live[i];", "x = local5->men;", "y = local5->gold;",
                                         "z = &local5->morale;"]
    twice = ["local5 = &province_table_live[i];", "local5 = 3;", "x = *(word*)((local5 + 16));"]
    assert L(twice) == twice


def test_ram_arrays_render_by_name_with_matching_declarations():
    """Labelled RAM arrays indexed by arithmetic render by array name, and each file declares them with
    the element type the shape implies. A literal index or a bare address is never touched, and an
    array used in two shapes in one file renders only the commoner one."""
    from emu_harness.vm.koei.nes import vm_decompile as VC
    labs = {0x77A8: ("daimyo_name_table", ""), 0x7BEA: ("battle_side_strength_mod", ""),
            0x6193: ("relations_matrix", ""), 0x6F4F: ("deduped_owner_list", "")}
    src = ("a = draw(((d * 9) + 0x77A8));\n"
           "b = *(word*)(((s << 1) + 0x7BEA));\n"
           "c = (((i * 8) + j) + 0x6193);\n"
           "e = f(0x6F4F, ((3 * 9) + 0x77A8));\n")
    out, decl = VC.render_ram_arrays(src, labs)
    assert out.splitlines()[:3] == ["a = draw(daimyo_name_table[d]);", "b = battle_side_strength_mod[s];",
                                    "c = &relations_matrix[((i * 8) + j)];"]
    assert out.splitlines()[3] == "e = f(0x6F4F, ((3 * 9) + 0x77A8));"
    assert decl.strip().splitlines() == ["extern word battle_side_strength_mod[];", "extern byte daimyo_name_table[][9];",
                                 "extern byte relations_matrix[];"]
    mixed, decl2 = VC.render_ram_arrays("x = ((d * 9) + 0x77A8);\ny = ((d * 9) + 0x77A8);\nz = (k + 0x77A8);\n", labs)
    assert mixed.splitlines() == ["x = daimyo_name_table[d];", "y = daimyo_name_table[d];", "z = (k + 0x77A8);"]
    assert decl2.strip() == "extern byte daimyo_name_table[][9];"


def test_record_pointer_calls_render_as_arrow_fields():
    """A routine that returns a record pointer ([<title>.structs] returned_by): F(x) + N reads as
    F(x)->field at the record's width; a bare call stays the pointer; an unnamed offset is left."""
    from emu_harness.vm.koei.nes import vm_decompile as VC
    rec = {"type": "daimyo", "base": 0x752F, "stride": 7, "width": "byte", "returned_by": ["dr"],
           "fields": {0: "age", 2: "drive", 4: "charisma"}}
    A = lambda t: VC.annotate_record_access(t, {}, rec)
    assert A("x = *(byte*)((dr(f(k)) + 4));") == "x = dr(f(k))->charisma;"
    assert A("x = *(byte*)(dr(k));") == "x = dr(k)->age;"
    assert A("p = (dr(k) + 2);") == "p = &dr(k)->drive;"
    for keep in ("p = dr(k);", "x = *(byte*)((dr(k) + 3));"):
        assert A(keep) == keep, keep
    # a read of another width keeps its cast over the field's address, as the province rule does
    assert A("x = *(word*)((dr(k) + 4));") == "x = *(word*)(&dr(k)->charisma);"


def test_batch_decompile_renders_the_var_walk_names(tmp_path):
    """titles.decompile (the path that regenerates a title's source/) must apply the registry's
    [vars.bankN."0xADDR"] frame-slot names. It did not from the engine's graduation until 2026-10-28:
    every slot printed positionally and the var walk's guard passed on an empty diff."""
    import pytest
    from emu_harness.vm.koei.nes import titles
    if not titles.game_cfg("na1")["_rom"].exists():
        pytest.skip("na1-decompiler ROM not present")
    from emu_harness.vm.koei.nes.vm_decompile import load_var_names
    vm = load_var_names(titles.game_cfg("na1")["_labels"])
    (bank, addr), names = next(((k, v) for k, v in sorted(vm.items()) if k[0] == 15 and "arg1" in v))
    asm, c = tmp_path / "b.asm", tmp_path / "b.c"
    titles.disasm("na1", bank, str(asm))
    titles.decompile("na1", bank, str(asm), str(c))
    text = c.read_text(encoding="utf-8")
    body = text[text.index(f"sub ${addr:04X}\n"):]
    head = next(ln for ln in body.splitlines() if ln.startswith("word "))
    assert f"word {names['arg1']}" in head, head


def test_coalesce_store_phis_folds_a_merge_store_back_into_its_variable():
    """A value-merge phi consumed by a store becomes the store's variable in every pred; a phi with any
    other reader (a branch, a return) is left alone."""
    from emu_harness.vm.koei.nes.vm_decompile import coalesce_store_phis
    lines = [(0x10, 1, "phi_val_0020 = 0;"), (0x10, 1, "goto L_0020;"), (0x18, 0, "L_0018:"),
             (0x18, 1, "phi_val_0020 = (i + 1);"), (0x20, 0, "L_0020:"), (0x20, 1, "i = phi_val_0020;"),
             (0x20, 1, "if (i < n) goto L_0018;")]
    out = coalesce_store_phis(lines)
    assert [t for _a, _i, t in out] == ["i = 0;", "goto L_0020;", "L_0018:", "i = (i + 1);", "L_0020:",
                                       "if (i < n) goto L_0018;"]
    assert [a for a, _i, _t in out] == [0x10, 0x10, 0x18, 0x18, 0x20, 0x20]      # every line keeps its block
    branch = lines[:5] + [(0x20, 1, "if (phi_val_0020) goto L_0018;")]
    assert coalesce_store_phis(branch) == branch


def test_rotated_loop_sets_its_variable_before_the_first_test(tmp_path):
    """A rotated loop's header runs on entry too. Lifting it into `while (C)` put its statements at the
    body bottom, so the printed C read a variable before setting it: avg_fief_tax_rate's `fief` (a phi
    merge store, 42 NA1 loops) and prompt_roll_stat_value's first roll (a header that does work, 9 more).
    The CFG was unchanged, so cfg_gate passed all 51 (2026-10-28)."""
    import pytest
    from emu_harness.vm.koei.nes import titles
    if not titles.game_cfg("na1")["_rom"].exists():
        pytest.skip("na1-decompiler ROM not present")
    asm, c = tmp_path / "b.asm", tmp_path / "b.c"
    titles.disasm("na1", 0, str(asm))
    titles.decompile("na1", 0, str(asm), str(c))
    text = c.read_text(encoding="utf-8")

    def body(addr):
        s = text[text.index(f"sub ${addr:04X}\n"):]
        return s[:s.index("\n}\n")]

    avg = body(0x952F)                                         # avg_fief_tax_rate
    assert "phi_val" not in avg
    assert avg.index("fief = 0;") < avg.index("while ("), avg
    roll = body(0x85FC)                                        # prompt_roll_stat_value
    # the roll IS the loop header ($8612): it must open the loop (under its label, or as the first work
    # of a `while (1) {  // $8612`), not be lifted below a `while (` at the body bottom
    head = roll.index("L_8612:") if "L_8612:" in roll else roll.index("{    // $8612")
    assert head < roll.index("rng_mod(50)") < roll.index("poll_input()", head), roll


def test_a_stored_call_is_read_back_by_name_not_called_again(tmp_path):
    """After `x = f();` a later read of the same value names x. Printing f() again reads as a second call
    (a second rng roll, a second prompt) where the bytecode called once: 94 NA1 sites (2026-10-28)."""
    import pytest
    from emu_harness.vm.koei.nes import titles
    if not titles.game_cfg("na1")["_rom"].exists():
        pytest.skip("na1-decompiler ROM not present")
    asm, c = tmp_path / "b.asm", tmp_path / "b.c"
    titles.disasm("na1", 0, str(asm))
    titles.decompile("na1", 0, str(asm), str(c))
    text = c.read_text(encoding="utf-8")
    roll = text[text.index("sub $85FC\n"):]
    roll = roll[:roll.index("\n}\n")]                          # prompt_roll_stat_value
    assert roll.count("rng_mod(50)") == 1, roll                # one CALL_abs_imm1 $CA52 at $8615
    assert "draw_message(msg_fmt__3d, rolled_value)" in roll, roll


def test_labeled_vm_listing_reads_by_name_and_keeps_every_address(tmp_path):
    """The reading view of the VM listing: a routine's name opens its block, `L_XXXX:` appears only where
    something branches, in-routine jumps name their label, and every instruction is indented under them
    with its address first, as in Mappy's labeled listing (the C's `// $XXXX` tags grep to it). The decompiler's input is a separate file and
    does not change."""
    import pytest, re
    from emu_harness.vm.koei.nes import titles
    if not titles.game_cfg("na1")["_rom"].exists():
        pytest.skip("na1-decompiler ROM not present")
    plain, lab = tmp_path / "b.asm", tmp_path / "b_labeled.asm"
    titles.disasm("na1", 15, str(plain))
    titles.disasm("na1", 15, str(lab), labeled=True)
    text = lab.read_text(encoding="utf-8")
    sub = text[text.index("deduct_byte_at:"):]
    sub = sub[:sub.index("\n\n")].splitlines()
    assert sub[0].startswith("deduct_byte_at:") and "sub $CA12" in sub[0]
    assert [ln for ln in sub if ln.startswith("L_")] == ["L_CA27:", "L_CA2F:"]   # the two branch targets only
    body = [ln for ln in sub[1:] if not ln.startswith("L_")]
    addrs = [re.match(r"^    ([0-9A-F]{4}): ", ln).group(1) for ln in body]
    assert addrs[0] == "CA17" and addrs[-1] == "CA30" and len(addrs) == 18        # every instruction, in order
    assert any("JUMPF_abs" in ln and "L_CA27" in ln for ln in body)
    assert "> $CA27" not in text and not re.search(r"^ [ >]\$", text, re.M)       # no address-led lines
    p = plain.read_text(encoding="utf-8")
    assert "; sub $CA12" in p and " >$CA27" in p                                  # decompiler input unchanged
