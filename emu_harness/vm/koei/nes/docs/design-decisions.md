# KOEI NES — cross-title design decisions ledger

> **Moved from koei-nes `docs/` on 2026-10-26** (koei-nes is being retired; its code moved to
> emu-harness in MIGRATION.md rows D-F, K, M, O). Kept verbatim. Tool paths below predate the move:
> `tools/koei_vm.py` = `emu_harness.vm.koei.nes.titles`, `tools/games.toml` =
> `emu_harness/vm/koei/nes/games.toml`, `tools/dump_data.py` = `emu_harness.vm.koei.nes.romdata`,
> `native_floor` / `bc_match` / `native_match` = `emu_harness.vm.koei.nes.native_floor` / `.match`,
> and the VM core is no longer imported from na1-decompiler.

> A comparative study, not an evolutionary tree. KOEI shipped ~8–9 strategy titles on the Famicom/NES
> on a **shared engine** (VM + BIOS + 32-bit ALU + library — see [shared-library.md](./shared-library.md)),
> but the game layer and many engine *policies* were re-decided each time, plausibly by **different teams**.
> So we do **not** assume a straight line release-to-release. The interesting result is divergence: e.g. if
> Genghis Khan (3rd) borrows more from ROTK than from Nobunaga's Ambition, that falsifies "linear refinement."
>
> Each entry is an **axis** along which titles made a measurable choice. Method mirrors the vault's
> specimen↔thesis split: each title is a dated specimen; the cross-title pattern is the portable thesis.
> Fill the table as ROMs are decoded; leave cells blank rather than guessing.

## Axis 1 — cross-bank call granularity (flexibility vs locality)

**The shared coarse mechanism (identical everywhere seen):** `call_bank` = BIOS syscall 7
(`set_prg_bank(N); jsr $8000; restore`) enters a whole bank as a **mode**. Bank 0 is the main loop;
it dispatches into the command/combat banks as engines. (NA1 `vm_bootstrap $A778`, ROTK boot→`$80D0`.)

**The divergence — a fine-grained "far call":** ROTK adds `call_in_vm_context(bank, fn_ptr, args…)`
(built on `os_set/get_vm_context` syscalls): switch bank, call **any function** in it, return. NA1 has
**no such primitive** — its only cross-bank verbs are `call_bank` (whole mode) + `call_bank10_entry`
(one specialized audio helper). So NA1 *must* keep related code co-located; ROTK can scatter it across
banks (AI→bank 3, map→bank 12) and call freely — treating the 256K as a flat callable space.

| Title | mode-dispatch (`call_bank`) | general far-call (`call_in_vm_context`) | cross-bank sites / 1k C-lines | reads as |
|---|---|---|---|---|
| Nobunaga's Ambition | ✅ | ❌ absent | **2.56** | locality / speed |
| Romance 3 Kingdoms | ✅ | ✅ (105 sites, banks 3 & 12) | **7.75** | flexibility / modularity |
| Genghis Khan | ? | ? | ? | ? |
| … | | | | |

**Cost basis (so "flexibility" has a price tag):** `set_prg_bank` early-outs if already current (~6 cyc),
else ~60 cyc (MMC1 5-bit serial write). `call_in_vm_context` is *interpreted bytecode* wrapping 3 syscalls
+ **2 real switches** (out & back) ≈ **500–800 cyc/call** (~2–3% of an NTSC frame). Cheap because ROTK
spends it almost entirely in cold, human-paced paths (per-turn/command/AI); the one hot exception is the
combat flash-animation loop touching bank 12 per frame. So ROTK bought modular factoring for an overhead
it mostly never feels.

**Open question for the corpus:** is fine-grained far-calling a one-off (ROTK only), a lineage (ROTK→GK→…),
or team-correlated? Watch whether each title *has the primitive in its engine* vs *chooses to use it*.

## Axis 2 — zero-page usage / the interpretation tax

ROTK's native VM dispatch loop (`vm_entry $EEB4`) is **already ZP-optimal**: all VM registers are ZP
(`vm_sp $02/03`, `vm_fp $04/05`, `vm_ip $06/07`, scratch `$00-0D`); opcode fetch is `lda (vm_ip),y`
(ZP-indirect) and dispatch is `jmp ($00)` (ZP-indirect). The only absolutes are the two 256-byte handler
tables (`$F6B7/$F7B7`, can't be ZP) and a forced `lda $0000,y` frame-copy (`LDA zp,y` has no opcode).

**Consequence — there is no ZP speedup to harvest, and a ROM fork is predicted to show ~0 delta**, for a
structural reason: the VM has **uniform 16-bit-operand addressing** with no ZP fast-path opcode, so a
LOAD/STORE to `$0050` and to `$7B8F` cost the same indirect access. Moving game vars into ZP wouldn't speed
up the bytecode touching them. The real cost is the **interpretation tax** — ~30–50 cyc/op of
fetch+dispatch+operand-decode wrapping ~4 cyc of work — which ZP can't touch. The instructive experiment is
not "move to ZP" but "instrument cycles-per-bytecode-op" to price the bytecode-engine choice itself.
(Reserved per-title: confirm each engine's dispatch loop is ZP-optimal; watch for a title that adds a
short/ZP-operand opcode — that would be a real divergence.)

## Axis 3 — strategic-AI paradigm (centralized planner vs decentralized agents)

| Title | AI locus | structure | strategy |
|---|---|---|---|
| Nobunaga's Ambition | **centralized**: one faction planner (`ai_strategic_turn_planner $A455`) | stateful coroutine (turn-flags + event-select + resume-flag; pauses for battle, resumes) | **planned** (global view) |
| Romance 3 Kingdoms | **decentralized**: 58 per-province agents (`ai_province_command_dispatcher $A4BE`) | stateless 3-way rule (build / expand / defend) on local wealth+threat | **emergent** (local rules) |
| Genghis Khan | ? | ? | ? |

A genuinely different *type* of AI, not a variation — and it's **downstream of the turn loop** (NA1
faction-centric → one planner; ROTK province-centric randomized → one agent per province). Tradeoff:
ROTK's agents are simple + robust but can't coordinate (two provinces may swing at the same target);
NA1's planner is complex but globally coherent. The discriminator to watch per title: is there a single
faction-turn planner, or N independent unit/province deciders? (Full ROTK writeup: ro3k `docs/04-ai-architecture.md`.)

## Axis 4 — income model (active command vs passive accrual)

| Title | gold income | rice income |
|---|---|---|
| Nobunaga's Ambition | explicit **Tax** command (menu: Move/War/**Tax**/…) | (harvest formula, town/output-based) |
| Romance 3 Kingdoms | **automatic** Fall harvest, `floor(256·rng/1000)` — flat & **stat-independent** (a dead-store BUG drops the intended stat term) | automatic Fall harvest, fully stat-driven (loyalty·pop·land) |

NA1 makes the player *spend a turn* to collect gold (Tax); ROTK automates it (one Fall harvest). And ROTK's
gold harvest carries a shipped **bug**: the stat-weighted term is computed then clobbered, so developing a
province grows its rice but not its gold — an asymmetric economy where gold comes from breadth + selling rice.
A clean specimen of a 1989 source-level `=`/`+=` slip surfaced by emulation (`ro3k tools/probe_gold.py`).

## Axis 5 — syscall dispatch mechanism (the engine self-dates here)

| | ROTK (game 1) | Nobunaga's Ambition (game 2) |
|---|---|---|
| `syscall_kind` | **`brk2`** -- BRK -> `irq_brk_dispatch $C13F` -> a **2-byte address table** (`syscall_dispatch_table $C16E`, 30 entries, x2) | **`jsr3`** -- JSR into a **3-byte JMP table** (`$C173`, 23 entries, x3) |
| interrupt handler | **assumes every IRQ is a syscall** -- no real interrupt path | **tests the pushed BRK status bit** -- a real INT handler was **retrofitted** |
| VM-side trampoline | `syscall_dispatch $F8B7` (== NA1 `$F226`): marshals the param block off the VM stack, dispatches via `jmp ($fffe)` | `syscall_dispatch $F226` |

NOT a difference: the per-service NAMED rendering (`syscall_memcpy_banked`, `syscall_wait_for_nmi`, ...) -- that's na1dream's decompiler naming and is identical in both once the entry is labeled; both also have ~12-13 `*_wrap` subs. The REAL divergence is the dispatch mechanism above.

**The engine self-dates.** You can only *add* the ability to distinguish a real IRQ from a syscall -- a team doesn't rip it out (NA1's label literally reads "retrofitted one"). So the bytecode *independently* orders the games ROTK -> NA1, matching the chronology (ROTK was KOEI's earlier title; the NES/Famicom port order agrees). The artifact is its own timestamp -- same shape as the atlas/data work, where running the data was the verifier.

**Correction to this ledger's premise:** for THIS axis it's a clean straight-line evolution (ROTK -> NA1), not divergence. The straight-line-vs-branch question stays open for **Genghis Khan (game 3)** -- the concrete probe is: diff GK's syscall dispatch against both. Does it keep evolving NA1's `jsr3`+real-INT line, or branch back toward ROTK's `brk2`? **GK is cued up next.**

## Axis N — (add per decode: record layouts, RNG, save format, combat model, terrain encoding, …)
