"""Probe the real provider: latency spread, limits, and what happens at a limit.

Raw events go to raw/real_profile.jsonl (one JSON object per HTTP exchange).
A summary used for Table 1 and for calibrating the stand-in goes to
raw/real_profile_summary.json.

Uses raw httpx on purpose (not the pipeline client), so the pipeline's own
limiter/retry logic cannot hide what the provider does.

    python -m relay.profile_real [phase ...]    # phases: baseline conc rpm tpm
"""

from __future__ import annotations

import asyncio
import json
import statistics as st
import sys
import time
from pathlib import Path

import httpx

from .config import ROOT, ProviderConfig

RAW = ROOT / "raw"
LOG = RAW / "real_profile.jsonl"
SUMMARY = RAW / "real_profile_summary.json"

WORDS = (
    "the model attends to tokens in the sequence and mixes information across positions using learned "
    "projections while residual connections and normalisation keep training stable across many layers "
).split()


def filler(n_tokens: int) -> str:
    # ~1.3 tokens per word for this vocabulary; calibrated below by comparing to usage.
    n_words = int(n_tokens / 1.3)
    return " ".join(WORDS[i % len(WORDS)] for i in range(n_words))


def log(ev: dict) -> None:
    RAW.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


class Prober:
    def __init__(self):
        self.p = ProviderConfig.groq()
        self.c = httpx.AsyncClient(
            base_url=self.p.base_url,
            headers={"Authorization": f"Bearer {self.p.api_key}"},
            timeout=httpx.Timeout(60.0),
            limits=httpx.Limits(max_connections=64),
        )
        self.t_base = time.time()

    async def call(self, phase: str, prompt_tokens: int, max_tokens: int, tag: str = "", instruction: str = "Reply with a short paragraph about it.") -> dict:
        body = {
            "model": self.p.model,
            "stream": True,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            **self.p.extra_body,
            "messages": [{"role": "user", "content": f"{instruction}\n\n{filler(prompt_tokens)}"}],
        }
        ev = {"phase": phase, "tag": tag, "t_wall": time.time() - self.t_base, "req_prompt_tokens": prompt_tokens, "max_tokens": max_tokens}
        t0 = time.perf_counter()
        ttft = None
        n_chunks = 0
        usage = None
        try:
            async with self.c.stream("POST", "/chat/completions", json=body) as r:
                ev["status"] = r.status_code
                ev["headers"] = {k: v for k, v in r.headers.items() if k.startswith("x-ratelimit") or k == "retry-after"}
                ev["t_headers_ms"] = (time.perf_counter() - t0) * 1000
                if r.status_code != 200:
                    ev["body"] = (await r.aread()).decode("utf-8", "replace")[:600]
                else:
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        d = line[5:].strip()
                        if d == "[DONE]":
                            break
                        j = json.loads(d)
                        u = j.get("usage") or (j.get("x_groq") or {}).get("usage")
                        if u:
                            usage = u
                        ch = j.get("choices") or []
                        if ch and ch[0].get("delta", {}).get("content"):
                            n_chunks += 1
                            if ttft is None:
                                ttft = (time.perf_counter() - t0) * 1000
        except Exception as e:  # noqa
            ev["exception"] = f"{type(e).__name__}: {e}"[:200]
        ev["total_ms"] = (time.perf_counter() - t0) * 1000
        ev["ttft_ms"] = ttft
        ev["chunks"] = n_chunks
        if usage:
            ev["usage"] = {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens", "queue_time", "prompt_time", "completion_time")}
        log(ev)
        return ev

    # -------------------------------------------------------------- phases
    async def baseline(self, n_each: int = 5):
        """Sequential requests across prompt sizes; paced by the remaining-tokens header."""
        sizes = [50, 500, 1500, 3000]
        outs = [60, 150, 250, 120]
        rem = 8000
        for i in range(n_each):
            for ps, mt in zip(sizes, outs):
                need = ps + mt + 60
                if rem < need * 1.3:
                    await asyncio.sleep(10)
                ev = await self.call("baseline", ps, mt, tag=f"p{ps}_m{mt}")
                h = ev.get("headers", {})
                try:
                    rem = int(h.get("x-ratelimit-remaining-tokens", rem))
                except ValueError:
                    pass
                print(f"baseline p={ps:5d} -> status {ev.get('status')} ttft={ev.get('ttft_ms') and round(ev['ttft_ms'])} total={round(ev['total_ms'])} rem_tok={rem}")
                await asyncio.sleep(1.0)

    async def conc(self, waves=(2, 4, 8, 12)):
        for k in waves:
            await asyncio.sleep(12)
            t0 = time.perf_counter()
            evs = await asyncio.gather(*[self.call("conc", 40, 30, tag=f"k{k}") for _ in range(k)])
            codes = [e.get("status") for e in evs]
            lat = sorted(round(e["total_ms"]) for e in evs)
            print(f"conc k={k}: statuses={dict((c, codes.count(c)) for c in set(codes))} lat(ms)={lat} wall={round((time.perf_counter()-t0)*1000)}")

    async def rpm(self, n: int = 45):
        """Wait out the minute window, then fire n tiny requests as fast as the client can."""
        print("rpm: idling 65s so earlier traffic leaves the window")
        await asyncio.sleep(65)
        evs = []
        t0 = time.perf_counter()
        sem = asyncio.Semaphore(6)  # keep in-flight modest so we test RPM, not concurrency

        async def one(i):
            async with sem:
                e = await self.call("rpm", 10, 1, tag=f"r{i}", instruction="Say OK.")
                e["i"] = i
                e["sent_at_s"] = time.perf_counter() - t0
                evs.append(e)

        await asyncio.gather(*[one(i) for i in range(n)])
        evs.sort(key=lambda e: e["t_wall"])
        first429 = next((k for k, e in enumerate(evs) if e.get("status") == 429), None)
        ok_before = sum(1 for e in evs[: first429 if first429 is not None else len(evs)] if e.get("status") == 200)
        print(f"rpm: {n} sent, 200s before first 429 = {ok_before}, first429 index={first429}")
        if first429 is not None:
            e = evs[first429]
            print("rpm 429 headers:", e.get("headers"), "body:", e.get("body"))
        # recovery polling
        t_start = time.perf_counter()
        rec = None
        for _ in range(40):
            await asyncio.sleep(2)
            e = await self.call("rpm_recover", 10, 1, tag="poll", instruction="Say OK.")
            if e.get("status") == 200:
                rec = time.perf_counter() - t_start
                break
        print("rpm recovery (s after burst end):", rec)

    async def tpm(self):
        print("tpm: idling 65s")
        await asyncio.sleep(65)
        for i in range(5):
            e = await self.call("tpm", 2400, 1, tag=f"big{i}", instruction="Say OK.")
            u = e.get("usage") or {}
            print(f"tpm big{i}: status={e.get('status')} prompt_tokens={u.get('prompt_tokens')} hdr={e.get('headers')} body={e.get('body')}")
            await asyncio.sleep(0.3)
        print("tpm: idling 65s then testing whether max_tokens is reserved against TPM")
        await asyncio.sleep(65)
        e = await self.call("tpm_reserve", 30, 7500, tag="max7500", instruction="Say OK.")
        print("max_tokens=7500:", e.get("status"), e.get("headers"), (e.get("usage") or {}).get("completion_tokens"), e.get("body"))
        e = await self.call("tpm_reserve", 30, 7500, tag="max7500_b", instruction="Say OK.")
        print("again immediately:", e.get("status"), e.get("headers"), e.get("body"))


def summarize() -> dict:
    evs = [json.loads(l) for l in LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    base = [e for e in evs if e["phase"] == "baseline" and e.get("status") == 200 and e.get("ttft_ms")]
    out: dict = {"n_events": len(evs), "n_baseline": len(base)}

    def pct(xs, q):
        xs = sorted(xs)
        if not xs:
            return None
        k = (len(xs) - 1) * q
        f, c = int(k), min(int(k) + 1, len(xs) - 1)
        return xs[f] + (xs[c] - xs[f]) * (k - f)

    if base:
        out["ttft_ms"] = {q: pct([e["ttft_ms"] for e in base], q / 100) for q in (50, 95, 99)}
        out["total_ms"] = {q: pct([e["total_ms"] for e in base], q / 100) for q in (50, 95, 99)}
        # model TTFT = a + b * prompt_tokens (least squares); decode speed from (total - ttft)/completion tokens
        xs = [e["usage"]["prompt_tokens"] for e in base if e.get("usage")]
        ys = [e["ttft_ms"] for e in base if e.get("usage")]
        if len(xs) >= 3:
            mx, my = st.mean(xs), st.mean(ys)
            b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / max(1e-9, sum((x - mx) ** 2 for x in xs))
            a = my - b * mx
            resid = [y - (a + b * x) for x, y in zip(xs, ys)]
            out["ttft_fit"] = {"a_ms": a, "b_ms_per_prompt_token": b, "resid_sd_ms": st.pstdev(resid), "resid_max_ms": max(resid)}
            import math

            ratios = [y / max(1.0, a + b * x) for x, y in zip(xs, ys)]
            out["ttft_ratio_lognorm_sigma"] = st.pstdev([math.log(max(r, 1e-3)) for r in ratios])
        dec = [(e["total_ms"] - e["ttft_ms"]) / max(1, e["usage"]["completion_tokens"] - 1) for e in base if e.get("usage") and e["usage"].get("completion_tokens", 0) > 20]
        if dec:
            out["decode_ms_per_token"] = {"p50": pct(dec, 0.5), "p95": pct(dec, 0.95)}
        # chars-per-token for the pipeline's token estimator is computed in the pipeline, not here
        qs = [e["usage"].get("queue_time") for e in base if e.get("usage") and e["usage"].get("queue_time") is not None]
        if qs:
            out["server_queue_time_s"] = {"p50": pct(qs, 0.5), "max": max(qs)}
    # limits
    firsts: dict = {}
    for ph in ("rpm", "tpm", "tpm_reserve", "conc", "rpm_recover"):
        es = [e for e in evs if e["phase"] == ph]
        e429 = [e for e in es if e.get("status") == 429]
        firsts[ph] = {
            "n": len(es),
            "n_429": len(e429),
            "ok_before_first_429": next((sum(1 for x in es[:k] if x.get("status") == 200) for k, x in enumerate(es) if x.get("status") == 429), None),
            "example_429": e429[0] if e429 else None,
        }
    out["limits"] = firsts
    hdrs = [e["headers"] for e in evs if e.get("headers")]
    if hdrs:
        out["header_limits_seen"] = {k: hdrs[-1].get(k) for k in ("x-ratelimit-limit-requests", "x-ratelimit-limit-tokens")}
    # does remaining-requests refill? (monotone check over wall time)
    rr = [(e["t_wall"], int(e["headers"]["x-ratelimit-remaining-requests"])) for e in evs if e.get("headers", {}).get("x-ratelimit-remaining-requests")]
    out["remaining_requests_series"] = [(round(t, 1), v) for t, v in rr[:: max(1, len(rr) // 40)]]
    out["total_tokens_spent_by_probe"] = sum((e.get("usage") or {}).get("total_tokens", 0) or 0 for e in evs)
    out["total_requests_by_probe"] = len(evs)
    return out


async def main(phases):
    pr = Prober()
    try:
        for ph in phases:
            await getattr(pr, ph)()
    finally:
        await pr.c.aclose()
    s = summarize()
    SUMMARY.write_text(json.dumps(s, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in s.items() if k not in ("remaining_requests_series", "limits")}, indent=2, default=str))


if __name__ == "__main__":
    phases = sys.argv[1:] or ["baseline", "conc", "rpm", "tpm"]
    asyncio.run(main(phases))
