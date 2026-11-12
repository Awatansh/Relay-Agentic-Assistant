"""Experiment runner: load scenarios, retrieval-setting evaluation, retry-storm experiment.

    python -m relay.runner standin            # all five stand-in load shapes
    python -m relay.runner real_load          # small real runs (steady, burst)
    python -m relay.runner quality real       # Table 3 on the real model
    python -m relay.runner quality standin    # same harness on the stand-in (free; plumbing check)
    python -m relay.runner retry_storm        # naive vs bounded retries
    python -m relay.runner smoke real         # 2 questions, to see the plumbing work

Every request writes one JSON line to raw/<scenario>.jsonl (spans, tokens, answer).
Reports are computed from those files only (relay.report).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

import httpx

from .client import LLMClient
from .config import ROOT, LimitsConfig, PipelineConfig, ProviderConfig, RetrievalConfig, RetryConfig
from .corpus import chunk_docs, load_docs
from .grading import grade, load_questions
from .limiter import Limiter
from .pipeline import LocalSearchTool, Pipeline
from .retriever import Retriever
from .trace import Trace, now

RAW = ROOT / "raw"
PORT = 8765


# ------------------------------------------------------------------ stand-in process
class StandIn:
    def __init__(self, seed: int = 11, port: int = PORT):
        self.port, self.seed, self.proc = port, seed, None
        self.base = f"http://127.0.0.1:{port}"

    def start(self) -> None:
        self.proc = subprocess.Popen([sys.executable, "-m", "relay.standin", "--port", str(self.port), "--seed", str(self.seed)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                if httpx.get(self.base + "/admin/state", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.25)
        raise RuntimeError("stand-in did not start")

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def reset(self, patch: dict | None = None, special: dict | None = None) -> dict:
        base = httpx.post(self.base + "/admin/reset", json={}).json()
        cal = RAW / "standin_calibration.json"
        cfgp = {k: v for k, v in (json.loads(cal.read_text()) if cal.exists() else {}).items() if not k.startswith("_")}
        httpx.post(self.base + "/admin/config", json={**cfgp, **(patch or {})})
        if special:
            httpx.post(self.base + "/admin/special", json=special)
        return httpx.get(self.base + "/admin/state").json()["cfg"]

    def state(self) -> dict:
        return httpx.get(self.base + "/admin/state").json()


# ------------------------------------------------------------------ scenarios
@dataclass
class Scenario:
    name: str
    backend: str  # real | standin
    n: int
    shape: str  # steady | burst | overload
    rate_per_min: float = 0.0  # steady / overload
    window_s: float = 0.0  # burst: all arrivals inside this window
    mix: str = "unique"  # unique | zipf
    standin: dict = field(default_factory=dict)  # stand-in config patch
    standin_special: dict = field(default_factory=dict)
    cache: bool = False
    tool: dict = field(default_factory=dict)
    note: str = ""

    @property
    def duration_s(self) -> float:
        return self.window_s if self.shape == "burst" else (self.n - 1) * 60.0 / self.rate_per_min if self.rate_per_min else 0.0

    @property
    def offered_per_min(self) -> float:
        d = self.duration_s
        return round(self.n / d * 60.0, 1) if d > 0 else float(self.n)


FAULTS = dict(fail_rate=0.02, hang_rate=0.01, slow_success_rate=0.02, tail_prob=0.03)

STANDIN_SCENARIOS = [
    Scenario("steady", "standin", 10, "steady", rate_per_min=3.0, standin=FAULTS, note="3 req/min, about what the free tier can serve with every reply fully checked"),
    Scenario("burst", "standin", 40, "burst", window_s=8.0, mix="zipf", cache=True, standin=FAULTS, note="40 requests inside 8 s, most asking the same few things (an incident)"),
    Scenario("sustained_overload", "standin", 60, "overload", rate_per_min=30.0, standin=FAULTS, note="30 req/min for 2 min: ~10x what fully-checked answers allow"),
    Scenario("rpm_halved", "standin", 20, "overload", rate_per_min=12.0, standin=FAULTS | dict(rpm=15, tpm=4000, itpm=3500), note="provider silently halves every per-minute limit"),
    Scenario("daily_exhausted", "standin", 20, "overload", rate_per_min=20.0, standin=FAULTS, standin_special=dict(rpd_level=6), note="only 6 requests left in the daily allowance when traffic starts"),
]
REAL_SCENARIOS = [
    Scenario("steady_real", "real", 12, "steady", rate_per_min=3.0, note="real Groq, 3 req/min, full tier"),
    Scenario("burst_real", "real", 10, "burst", window_s=3.0, mix="zipf", cache=True, note="real Groq, 10 requests in 3 s"),
]


# ------------------------------------------------------------------ assembly
_SHARED: dict = {}


def retrieval_assets(rc: RetrievalConfig, corpus_dir: str):
    key = (rc.chunk_chars, rc.overlap_chars, corpus_dir)
    if key not in _SHARED:
        if "docs" not in _SHARED:
            _SHARED["docs"] = load_docs(corpus_dir)
        chunks = chunk_docs(_SHARED["docs"], rc.chunk_chars, rc.overlap_chars)
        _SHARED[key] = Retriever(chunks, aliases={d.doc_id: list(d.aliases) for d in _SHARED['docs']})
    return _SHARED["docs"], _SHARED[key]


def build(provider: ProviderConfig, *, limits: LimitsConfig | None = None, pcfg: PipelineConfig | None = None, rc: RetrievalConfig | None = None, retry: RetryConfig | None = None, **pipe_kw):
    pcfg, rc = pcfg or PipelineConfig(), rc or RetrievalConfig()
    retry = retry or RetryConfig()
    docs, ret = retrieval_assets(rc, pcfg.corpus_dir)
    limiter = Limiter(limits or LimitsConfig())
    client = LLMClient(provider, retry, limiter, rng=random.Random(5))
    tool = LocalSearchTool(ret)
    pipe = Pipeline(client, limiter, ret, pcfg, rc, tool, **pipe_kw)
    return pipe, client, limiter, tool, docs


def schedule(s: Scenario, rng: random.Random) -> list[float]:
    if s.shape == "burst":
        return sorted(rng.uniform(0, s.window_s) for _ in range(s.n))
    gap = 60.0 / s.rate_per_min
    return [i * gap for i in range(s.n)]


def pick_questions(s: Scenario, qs, rng: random.Random):
    ans = list(qs)
    if s.mix == "zipf":
        w = [1.0 / (i + 1) ** 1.3 for i in range(len(ans))]
        return rng.choices(ans, weights=w, k=s.n)
    return [ans[i % len(ans)] for i in range(s.n)]


async def fire(pipe: Pipeline, q, offset: float, t0: float, name: str, i: int, patience: float, out: list):
    await asyncio.sleep(max(0.0, t0 + offset - now()))
    tr = Trace(request_id=f"{name}-{i:03d}", scenario=name, question=q.q)
    tr.meta["send_offset_s"] = now() - t0
    tr.meta["qid"] = q.id
    task = asyncio.create_task(pipe.handle(q.q, tr))
    done, _ = await asyncio.wait({task}, timeout=patience)
    if not done:  # the user has left
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        tr.error = "gave up waiting"
        tr.t_first_token = None
        tr.finish("given_up")
    else:
        exc = task.exception()
        if exc is not None:
            tr.error = f"{type(exc).__name__}: {exc}"[:200]
            if tr.t_end is None:
                tr.finish("failed")
    out.append(tr)


async def run_scenario(s: Scenario, standin: Optional[StandIn], *, retry: RetryConfig | None = None, admission: bool = True, seed: int = 3, out_name: str | None = None) -> dict:
    qs = load_questions()
    rng = random.Random(seed)
    if s.backend == "standin":
        cfg = standin.reset(s.standin, s.standin_special)
        provider = ProviderConfig.standin(standin.port)
    else:
        cfg = {}
        provider = ProviderConfig.groq()
    pcfg = PipelineConfig()
    pipe, client, limiter, tool, docs = build(provider, pcfg=pcfg, retry=retry, use_admission=admission, cache_ttl_s=300.0 if s.cache else 0.0)
    for k, v in s.tool.items():
        setattr(tool, k, v)
    offsets = schedule(s, rng)
    questions = pick_questions(s, qs, rng)
    out: list[Trace] = []
    t0 = now()
    await asyncio.gather(*[fire(pipe, q, off, t0, s.name, i, pcfg.patience_s, out) for i, (q, off) in enumerate(zip(questions, offsets))])
    wall = now() - t0
    await client.aclose()
    out.sort(key=lambda t: t.request_id)
    name = out_name or s.name
    RAW.mkdir(exist_ok=True)
    with (RAW / f"{name}.jsonl").open("w", encoding="utf-8") as f:
        for tr in out:
            d = json.loads(tr.to_json())
            q = next(x for x in qs if x.id == tr.meta.get("qid"))
            lab, why = grade(q, tr.answer, tr.meta.get("answer_outcome", tr.outcome), docs)
            d["grade"], d["grade_reason"] = lab, why
            d["qid"] = q.id
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    meta = {
        "scenario": s.name, "backend": s.backend, "requests": s.n, "offered_per_min": s.offered_per_min, "planned_duration_s": round(s.duration_s, 1),
        "wall_s": round(wall, 1), "peak_queue": limiter.peak_queue, "client_stats": client.stats, "limiter_429_events": limiter.events[:50],
        "limits_after": {"rpm": limiter.rpm, "tpm": limiter.tpm, "itpm": limiter.itpm, "rpd": limiter.rpd}, "pipeline_counters": pipe.counters,
        "standin_cfg": {k: cfg[k] for k in ("rpm", "tpm", "itpm", "fail_rate", "hang_rate", "slow_success_rate")} if cfg else None,
        "standin_counts": standin.state()["counts"] if s.backend == "standin" else None, "note": s.note, "admission": admission,
        "retry": retry.__dict__ if retry else RetryConfig().__dict__,
    }
    (RAW / f"{name}.meta.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    return meta


# ------------------------------------------------------------------ retrieval settings
SETTINGS = {
    "A": RetrievalConfig(chunk_chars=500, overlap_chars=80, top_k=6, rerank=False),
    "B": RetrievalConfig(chunk_chars=900, overlap_chars=150, top_k=4, rerank=True),
    "C": RetrievalConfig(chunk_chars=1500, overlap_chars=200, top_k=3, rerank=True),
    "D": RetrievalConfig(chunk_chars=900, overlap_chars=150, top_k=4, rerank=False),
    "E": RetrievalConfig(chunk_chars=900, overlap_chars=150, top_k=2, rerank=True),
    "F": RetrievalConfig(chunk_chars=500, overlap_chars=80, top_k=3, rerank=False),
}


async def run_quality(setting: str, backend: str, standin: Optional[StandIn], qids: Optional[list[str]] = None, tier: str = "lean") -> dict:
    rc = SETTINGS[setting]
    qs = [q for q in load_questions() if not qids or q.id in qids]
    pcfg = PipelineConfig(patience_s=120.0, hard_timeout_s=240.0)  # evaluation: fixed tier, wait for capacity instead of degrading
    if backend == "standin":
        standin.reset({"tail_prob": 0.0}, None)
        provider = ProviderConfig.standin(standin.port)
    else:
        provider = ProviderConfig.groq()
    retry = replace(RetryConfig(), attempt_timeout_s=20.0)
    pipe, client, limiter, tool, docs = build(provider, pcfg=pcfg, rc=rc, retry=retry)
    pipe.force_tier = tier
    name = f"quality_{backend}_{tier}_{setting}"
    rows = []
    t_start = now()
    for i, q in enumerate(qs):
        tr = Trace(request_id=f"{name}-{q.id}", scenario=name, question=q.q)
        tr.meta["send_offset_s"] = now() - t_start
        tr.meta["qid"] = q.id
        try:
            await pipe.handle(q.q, tr)
        except Exception as e:  # noqa: BLE001
            tr.error = f"{type(e).__name__}: {e}"[:200]
            if tr.t_end is None:
                tr.finish("failed")
        d = json.loads(tr.to_json())
        d["qid"] = q.id
        d["grade"], d["grade_reason"] = grade(q, tr.answer, tr.meta.get("answer_outcome", tr.outcome), docs)
        d["setting"] = setting
        d["setting_desc"] = f"{rc.chunk_chars} chars, {rc.top_k} pieces, {'rerank' if rc.rerank else 'no rerank'}"
        d["pipeline_tier"] = tier
        d["ttft_excl_admission_ms"] = None if tr.ttft_ms is None else round(tr.ttft_ms - tr.sum_ms("wait"), 1)
        d["total_excl_admission_ms"] = None if tr.total_ms is None else round(tr.total_ms - tr.sum_ms("wait"), 1)
        rows.append(d)
        print(f"  {name} {q.id} {d['grade']:18s} tier={tr.tier:7s} tok={tr.tokens_in + tr.tokens_out:5d} ttft*={d['ttft_excl_admission_ms']} {d['grade_reason'][:50]}", flush=True)
    await client.aclose()
    with (RAW / f"{name}.jsonl").open("w", encoding="utf-8") as f:
        for d in rows:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return {"setting": setting, "rows": len(rows)}


def retrieval_offline(settings=None) -> list[dict]:
    """Free (no model): does the *retrieved set* contain the evidence? Used to choose which settings to run live."""
    from .grading import Q  # noqa
    import re

    qs = [q for q in load_questions() if q.kind == "answerable"]
    out = []
    for name, rc in (settings or SETTINGS).items():
        docs, ret = retrieval_assets(rc, str(ROOT / "corpus"))
        hit = 0
        toks = 0
        for q in qs:
            hits = ret.search(q.q, rc.top_k, rc.rerank)
            toks += sum(len(h.chunk.text) for h in hits) / 3.8
            # all expected facts must be present in the retrieved text, and every expected paper represented
            blob = " ".join(re.sub(r"\s+", " ", h.chunk.text) for h in hits)
            ok = all(any(re.search(p, blob, re.I) for p in grp) for grp in q.must) and all(any(key in h.chunk.doc_id for h in hits) for key in q.docs)
            hit += ok
        out.append({"setting": name, "desc": f"{rc.chunk_chars} chars, {rc.top_k} pieces, {'rerank' if rc.rerank else 'no rerank'}", "evidence_in_retrieved_%": round(100 * hit / len(qs), 1), "avg_passage_tokens": round(toks / len(qs))})
    return out


# ------------------------------------------------------------------ retry storm
async def run_retry_storm(standin: StandIn):
    """Same overload, same stand-in; clients talk to the provider directly (no admission control) with two retry policies."""
    naive = RetryConfig(max_attempts=6, base_delay_s=1.0, max_delay_s=2.0, jitter=False, honor_retry_after=False, use_budget=False, cooldown_on_429=False, attempt_timeout_s=12.0)
    bounded = RetryConfig()  # jitter, retry-after, budget, shared cooldown
    sc = Scenario("storm", "standin", 40, "burst", window_s=6.0, mix="unique", standin=FAULTS | dict(tpm=8000), note="40 requests in 6 s, admission control OFF")
    res = {}
    for label, rcfg in (("naive_retry", naive), ("bounded_retry", bounded)):
        await asyncio.sleep(1)
        meta = await run_scenario(replace(sc, name=f"storm_{label}"), standin, retry=rcfg, admission=False, out_name=f"storm_{label}")
        res[label] = meta
    return res


async def smoke(backend: str, standin: Optional[StandIn]):
    s = Scenario("smoke", backend, 2, "steady", rate_per_min=30.0)
    meta = await run_scenario(s, standin, out_name=f"smoke_{backend}")
    for l in (RAW / f"smoke_{backend}.jsonl").read_text(encoding="utf-8").splitlines():
        d = json.loads(l)
        print(d["request_id"], d["outcome"], d["tier"], d["ttft_ms"], d["total_ms"], d["tokens_in"], d["tokens_out"], d["grade"], d["grade_reason"])
        print("  ", d["answer"][:300].replace("\n", " | "))
        print("   spans:", [(x["kind"], x["name"], x["start_ms"], x["end_ms"]) for x in d["spans"]])


async def main_async(args):
    standin = StandIn()
    need_standin = args.cmd in ("standin", "retry_storm") or (args.cmd in ("quality", "smoke") and args.arg == "standin")
    if need_standin:
        standin.start()
    try:
        if args.cmd == "smoke":
            await smoke(args.arg or "standin", standin if need_standin else None)
        elif args.cmd == "standin":
            for s in STANDIN_SCENARIOS:
                if args.only and s.name not in args.only:
                    continue
                m = await run_scenario(s, standin)
                print("done", s.name, "wall", m["wall_s"], "peak_queue", m["peak_queue"], m["standin_counts"], flush=True)
        elif args.cmd == "real_load":
            for s in REAL_SCENARIOS:
                if args.only and s.name not in args.only:
                    continue
                print("idle 65 s so earlier traffic leaves the provider's minute window", flush=True)
                await asyncio.sleep(65)
                m = await run_scenario(s, None)
                print("done", s.name, "wall", m["wall_s"], "peak_queue", m["peak_queue"], m["client_stats"], flush=True)
        elif args.cmd == "quality":
            qids = [q.id for q in load_questions()]
            if args.subset:
                qids = args.subset
            for st in (args.settings or ["B", "A", "C"]):
                if args.arg == "real":
                    print("idle 65 s", flush=True)
                    await asyncio.sleep(65)
                await run_quality(st, args.arg, standin if need_standin else None, qids, tier=args.tier)
        elif args.cmd == "retry_storm":
            await run_retry_storm(standin)
        elif args.cmd == "offline":
            for r in retrieval_offline():
                print(r)
    finally:
        standin.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["smoke", "standin", "real_load", "quality", "retry_storm", "offline"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--settings", nargs="*")
    ap.add_argument("--subset", nargs="*")
    ap.add_argument("--tier", default="lean", choices=["lean", "full"])
    asyncio.run(main_async(ap.parse_args()))


