"""Segment search: grow an input log one segment at a time from savestates.

From a checkpoint, run N sampled input segments (in parallel, one emulator per worker),
judge each with the game adapter, keep the best survivor as the next checkpoint, and back
up one segment when every candidate dies. Every kept segment is appended to one input log
that starts at power-on, so the result replays from scratch (the independent check: a
fresh instance, no savestates).

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
    state: str
    log_len: int                # input-log length (frames since power-on) at this state
    obs: Any
    score: float
    fails: int = 0              # times every candidate from here died


@dataclass
class Result:
    log: list[int]
    checkpoints: list[Checkpoint]
    goal: bool
    candidates_run: int
    backtracks: int
    seconds: float
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
            fn, out = job
            try:
                out.put(fn(client))
            except Exception as e:      # surface worker errors to the caller
                out.put(e)

    def map(self, fn_list):
        out: queue.Queue = queue.Queue()
        for fn in fn_list:
            self.jobs.put((fn, out))
        results = [out.get() for _ in fn_list]
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


def segment_search(make_client, adapter, *, start_state: str, start_log: list[int],
                   candidates: int = 32, workers: int = 8, max_segments: int = 1000,
                   max_fails: int = 3, seed: int = 0, max_seconds: float = 3600,
                   probe_top: int = 4, probe_samples: int = 8,
                   progress: Callable[[str], None] = print) -> Result:
    """start_state/start_log: a savestate and the power-on input log that reaches it."""
    rng = random.Random(seed)
    pool = _Pool(make_client, workers)
    t0 = time.perf_counter()
    run = backtracks = 0
    history: list[str] = []
    try:
        def observe_start(c):
            c.load(start_state)
            return adapter.observe(c)
        base_obs = pool.map([observe_start])[0]
        stack = [Checkpoint(start_state, len(start_log), base_obs, 0.0)]
        segs: list[list[int]] = []          # segs[i] leads from stack[i] to stack[i+1]
        goal = False
        while len(segs) < max_segments and time.perf_counter() - t0 < max_seconds:
            top = stack[-1]
            samples = [adapter.sample(rng) for _ in range(candidates)]

            def job(masks, st=top.state, base=top.obs):
                def fn(c):
                    c.load(st)
                    c.run_masks(masks)
                    obs = adapter.observe(c)
                    return masks, obs, adapter.judge(obs, base), c.save()
                return fn

            results = pool.map([job(m) for m in samples])
            run += len(results)
            alive = [r for r in results if not r[2].dead]
            alive.sort(key=lambda r: r[2].score, reverse=True)
            pick = alive[0] if alive else None
            if alive and probe_top and probe_samples and not alive[0][2].goal:
                # Look ahead: a candidate that survives its own segment can still end where
                # every continuation dies (and greedy re-picks it after each backtrack).
                # Keep the best-scoring of the top few that has a surviving continuation.
                top_k = alive[:probe_top]

                def probe(st, obs0, masks):
                    def fn(c):
                        c.load(st)
                        c.run_masks(masks)
                        return not adapter.judge(adapter.observe(c), obs0).dead
                    return fn

                jobs = [probe(r[3], r[1], adapter.sample(rng)) for r in top_k for _ in range(probe_samples)]
                ok = pool.map(jobs)
                run += len(jobs)
                pick = next((r for i, r in enumerate(top_k)
                             if any(ok[i * probe_samples:(i + 1) * probe_samples])), None)
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
                    msg = f"  dead end x{max_fails}; back up to segment {len(segs)}"
                    progress(msg)
                    history.append(msg)
                continue
            masks, obs, verdict, state = pick
            segs.append(masks)
            stack.append(Checkpoint(state, top.log_len + len(masks), obs, verdict.score))
            msg = (f"seg {len(segs):>4}  frame {stack[-1].log_len:>6}  alive {len(alive):>2}/{candidates}"
                   f"  {adapter.describe(obs)}  [{time.perf_counter() - t0:.0f}s]")
            progress(msg)
            history.append(msg)
            if verdict.goal:
                goal = True
                break
        log = list(start_log) + [m for s in segs for m in s]
        return Result(log, stack, goal, run, backtracks, time.perf_counter() - t0, history)
    finally:
        pool.close()
