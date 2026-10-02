"""Segment search: grow an input log one segment at a time from savestates.

From a checkpoint, run N sampled input segments (in parallel, one emulator per worker),
judge each with the game adapter, keep the best survivor as the next checkpoint, and back
up when every candidate dies. Kept segments form one input sequence that, after each
seed's boot prefix, replays from power-on (the independent check: a fresh instance, no
savestates).

Seeds. A checkpoint holds one savestate per *seed*: the same game position reached from
different hidden state (e.g. a frame-counter phase). A candidate is played on every seed
with the same inputs; it survives only if it survives on all of them, scores its worst
seed, and reaches the goal only when every seed does. One seed = a TAS for that seed;
several = a policy that does not depend on the seed.

Time. When a candidate reaches the goal, its segment is cut at the first frame where the
goal holds on every seed, and goal candidates rank by that frame. `search(bound=...)`
abandons a run as soon as it cannot beat a known time, so restarts with fresh search
seeds only ever improve the best.

Emulator- and game-agnostic. The caller supplies:
  make_client()      -> a started client with save/load/run_masks/close
  adapter.sample(rng)               -> list[int]: one mask per frame for a candidate segment
  adapter.observe(client)           -> an observation of the settled state
  adapter.judge(obs, base_obs)      -> Verdict for a candidate (base_obs = the checkpoint's)
  adapter.describe(obs)             -> short text for progress lines
"""
from __future__ import annotations

import queue
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Verdict:
    score: float
    dead: bool = False
    goal: bool = False          # the adapter's stop condition was reached


@dataclass
class Checkpoint:
    states: list[str]           # one savestate per seed
    frames: int                 # frames played since the start states
    obs: list[Any]              # one observation per seed
    score: float
    fails: int = 0              # times every candidate from here died


@dataclass
class Result:
    inputs: list[int]           # one mask per frame from the start states (same for every seed)
    checkpoints: list[Checkpoint]
    goal: bool
    goal_frame: int | None      # frames from the start states to the goal, if reached
    candidates_run: int
    backtracks: int
    seconds: float
    pruned: bool = False        # stopped because it could not beat the bound
    history: list[str] = field(default_factory=list)


class _Pool:
    """Worker threads, each owning one emulator instance."""

    def __init__(self, make_client: Callable[[], Any], workers: int):
        self.jobs: queue.Queue = queue.Queue()
        self.clients = [make_client() for _ in range(workers)]
        self.threads = [threading.Thread(target=self._run, args=(c,), daemon=True) for c in self.clients]
        for t in self.threads:
            t.start()

    def _run(self, client):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            fn, i, out = job
            try:
                out.put((i, fn(client)))
            except Exception as e:      # surface worker errors to the caller
                out.put((i, e))

    def map(self, fns):
        out: queue.Queue = queue.Queue()
        for i, fn in enumerate(fns):
            self.jobs.put((fn, i, out))
        results = [None] * len(fns)
        for _ in fns:
            i, r = out.get()
            results[i] = r
        for r in results:
            if isinstance(r, Exception):
                raise r
        return results

    def close(self):
        for _ in self.threads:
            self.jobs.put(None)
        for t in self.threads:
            t.join(timeout=30)
        for c in self.clients:
            c.close()


class Searcher:
    """Owns the emulator pool, so restarts reuse running instances."""

    def __init__(self, make_client, adapter, *, workers: int = 8):
        self.adapter = adapter
        self.pool = _Pool(make_client, workers)

    def close(self):
        self.pool.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- one candidate on every seed ----------------------------------------------
    def _play(self, cp: Checkpoint, masks_list: list[list[int]], keep_state: bool):
        """Play each mask list from every seed of cp. Returns per list: [(obs, verdict, state)]."""
        ad = self.adapter

        def job(st, base, masks):
            def fn(c):
                c.load(st)
                c.run_masks(masks)
                obs = ad.observe(c)
                return obs, ad.judge(obs, base), (c.save() if keep_state else None)
            return fn

        n = len(cp.states)
        flat = self.pool.map([job(st, base, m) for m in masks_list
                              for st, base in zip(cp.states, cp.obs)])
        return [flat[i * n:(i + 1) * n] for i in range(len(masks_list))]

    def _goal_frame(self, cp: Checkpoint, masks: list[int]) -> int:
        """First frame count (1-based, within masks) at which the goal holds on every seed."""
        ad = self.adapter

        def job(st, base):
            def fn(c):
                c.load(st)
                for f, m in enumerate(masks, 1):
                    c.run_masks([m])
                    if ad.judge(ad.observe(c), base).goal:
                        return f
                return len(masks)
            return fn

        return max(self.pool.map([job(st, base) for st, base in zip(cp.states, cp.obs)]))

    def _cut(self, cp: Checkpoint, masks: list[int]):
        """Re-play masks on every seed and keep the end states (for a trimmed goal segment)."""
        (per_seed,) = self._play(cp, [masks], keep_state=True)
        return per_seed

    # -- the search -----------------------------------------------------------------
    def search(self, start_states: list[str], *, candidates: int = 32, max_segments: int = 1000,
               max_fails: int = 3, seed: int = 0, max_seconds: float = 3600,
               probe_top: int = 4, probe_samples: int = 8, bound: int | None = None,
               progress: Callable[[str], None] = print) -> Result:
        """start_states: one savestate per seed, all at the same game position.
        bound: a known goal frame to beat; the run stops once it cannot."""
        ad = self.adapter
        rng = random.Random(seed)
        t0 = time.perf_counter()
        run = backtracks = 0
        history: list[str] = []

        def say(msg):
            progress(msg)
            history.append(msg)

        def observe_start(st):
            def fn(c):
                c.load(st)
                return ad.observe(c)
            return fn

        base_obs = self.pool.map([observe_start(st) for st in start_states])
        stack = [Checkpoint(list(start_states), 0, base_obs, 0.0)]
        segs: list[list[int]] = []          # segs[i] leads from stack[i] to stack[i+1]
        goal_frame = None
        pruned = False
        while len(segs) < max_segments and time.perf_counter() - t0 < max_seconds:
            top = stack[-1]
            if bound is not None and top.frames >= bound:
                pruned = True
                say(f"  frame {top.frames} reached the bound {bound} without the goal; stop")
                break
            samples = [ad.sample(rng) for _ in range(candidates)]
            results = self._play(top, samples, keep_state=True)
            run += len(results) * len(top.states)
            alive = []
            for masks, per_seed in zip(samples, results):
                vs = [v for _, v, _ in per_seed]
                if any(v.dead for v in vs):
                    continue
                score = min(v.score for v in vs)
                alive.append((score, all(v.goal for v in vs), masks, per_seed))
            pick = None
            goals = [a for a in alive if a[1]]
            if goals:
                # Time: among candidates that reach the goal, the earliest goal frame wins.
                timed = [(self._goal_frame(top, a[2]), a) for a in goals]
                f, a = min(timed, key=lambda t: t[0])
                masks = a[2][:f]
                per_seed = self._cut(top, masks)
                pick = (a[0], True, masks, per_seed)
            elif alive:
                alive.sort(key=lambda a: a[0], reverse=True)
                pick = alive[0]
                if probe_top and probe_samples:
                    # Look ahead: a candidate that survives its own segment can still end where
                    # every continuation dies (and greedy re-picks it after each backtrack).
                    # Keep the best-scoring of the top few with a continuation that survives
                    # on every seed.
                    top_k = alive[:probe_top]
                    probes = [ad.sample(rng) for _ in range(probe_samples)]
                    ok_for = []
                    for a in top_k:
                        cp = Checkpoint([s for _, _, s in a[3]], 0, [o for o, _, _ in a[3]], 0.0)
                        res = self._play(cp, probes, keep_state=False)
                        run += len(probes) * len(cp.states)
                        ok_for.append(any(not any(v.dead for _, v, _ in r) for r in res))
                    pick = next((a for a, ok in zip(top_k, ok_for) if ok), None)
            if pick is None:
                top.fails += 1
                if top.fails >= max_fails and len(stack) > 1:
                    # A dead end also counts against its parent, so a trap set several
                    # segments back (re-chosen greedily each time) escalates the backtrack.
                    while len(stack) > 1 and stack[-1].fails >= max_fails:
                        stack.pop()
                        segs.pop()
                        stack[-1].fails += 1
                        backtracks += 1
                    say(f"  dead end x{max_fails}; back up to segment {len(segs)}")
                continue
            score, is_goal, masks, per_seed = pick
            segs.append(masks)
            obs = [o for o, _, _ in per_seed]
            stack.append(Checkpoint([s for _, _, s in per_seed], top.frames + len(masks), obs, score))
            worst = min(range(len(obs)), key=lambda i: per_seed[i][1].score)
            say(f"seg {len(segs):>4}  frame {stack[-1].frames:>6}  alive {len(alive):>2}/{candidates}"
                f"  {ad.describe(obs[worst])}  [{time.perf_counter() - t0:.0f}s]")
            if is_goal:
                goal_frame = stack[-1].frames
                break
        inputs = [m for s in segs for m in s]
        return Result(inputs, stack, goal_frame is not None, goal_frame, run, backtracks,
                      time.perf_counter() - t0, pruned, history)
