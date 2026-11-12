"""Stand-in model service: an OpenAI-compatible HTTP server we control.

It can be slow, fail, hang, refuse because a limit was reached, slow down under
load, and stream its reply a piece at a time. Latency parameters are fitted from
raw/real_profile.jsonl by `calibrate()`; limit behaviour copies what the probes
saw on Groq (429 body wording, retry-after, x-ratelimit-* headers, hidden ITPM).

Content is *extractive* and corpus-agnostic: it never knows the papers. The
answer step quotes the best-matching sentences from the passages it is given
(so retrieval quality still matters), the verify step flags a configurable
share of drafts, the understand step returns the question's keywords. It is
NOT a language model: do not read answer quality off it.

    python -m relay.standin --port 8765 [--seed 1]
Admin: POST /admin/config {json patch}, GET /admin/state, POST /admin/reset
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import statistics as st
import time
from collections import deque
from pathlib import Path

from aiohttp import web

from .config import ROOT
from .retriever import tokenize

RAW = ROOT / "raw"

DEFAULTS = dict(
    # --- latency model (overwritten by calibration) ---
    ttft_a_ms=314.0,
    ttft_b_ms_per_tok=0.115,
    ttft_sigma=0.42,
    decode_ms_per_tok=2.2,
    decode_sigma=0.25,
    load_slope=0.07,  # latency multiplier grows by this per concurrent request above load_free
    load_free=1,
    tail_prob=0.03,  # "slow for no visible reason": TTFT multiplied by tail_mult
    tail_mult=6.0,
    # --- faults ---
    fail_rate=0.0,  # immediate HTTP 503
    hang_rate=0.0,  # accept, then never answer (client must time out)
    slow_success_rate=0.0,  # succeed, but only after slow_success_extra_ms
    slow_success_extra_ms=6000.0,
    # --- limits (Groq qwen free tier as measured) ---
    rpm=30,
    tpm=8000,
    itpm=7000,
    rpd=1000,
    tpd=200000,
    chars_per_token=3.8,
    verify_flag_rate=0.15,
    max_inflight=64,  # real service: >=12 observed OK
)


def fmt_dur(s: float) -> str:
    s = max(0.0, s)
    if s < 1:
        return f"{int(round(s * 1000))}ms"
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    out = (f"{int(h)}h" if h else "") + (f"{int(m)}m" if (m or h) else "")
    return out + (f"{sec:.1f}s" if (h or m) else f"{sec:.2f}".rstrip("0").rstrip(".") + "s")


class State:
    def __init__(self, seed: int | None):
        self.cfg = dict(DEFAULTS)
        self.rng = random.Random(seed)
        self.reset()

    def reset(self):
        self.req_win: deque = deque()
        self.in_win: deque = deque()
        self.tot_win: deque = deque()
        self.rpd_level = float(self.cfg["rpd"])
        self.tpd_level = float(self.cfg["tpd"])
        self.t_bucket = time.monotonic()
        self.inflight = 0
        self.counts = dict(total=0, ok=0, r429=0, fail=0, hang=0, max_inflight=0)

    def refill(self, t):
        dt = t - self.t_bucket
        self.t_bucket = t
        self.rpd_level = min(self.cfg["rpd"], self.rpd_level + dt * self.cfg["rpd"] / 86400)
        self.tpd_level = min(self.cfg["tpd"], self.tpd_level + dt * self.cfg["tpd"] / 86400)
        for w in (self.req_win, self.in_win, self.tot_win):
            while w and t - w[0][0] >= 60:
                w.popleft()


def pct(xs, q):
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    f, c = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


# --------------------------------------------------------------------- content
def _task(system: str) -> str:
    m = re.search(r"\[\[(UNDERSTAND|ANSWER|REVISE|VERIFY)\]\]", system)
    return m.group(1) if m else "CHAT"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 25]


def make_content(st_: State, system: str, user: str) -> str:
    task = _task(system)
    if task == "UNDERSTAND":
        kws = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", user) if len(w) > 2]
        return json.dumps({"search_query": " ".join(kws[:12]), "scope": "in", "papers": []})
    if task == "VERIFY":
        if st_.rng.random() < st_.cfg["verify_flag_rate"]:
            return json.dumps({"verdict": "REVISE", "issues": ["claim not directly supported by cited passage"]})
        return json.dumps({"verdict": "PASS", "issues": []})
    # ANSWER / REVISE: extractive
    passages = re.findall(r"\[(S\d+)\]\s*\(([^)]*)\)\s*(.*?)(?=\n\[S\d+\]|\Z)", system, flags=re.S)
    q = set(tokenize(user))
    cands = []
    for sid, where, txt in passages:
        for s in _sentences(txt):
            ov = len(q & set(tokenize(s)))
            cands.append((ov, sid, s))
    cands.sort(key=lambda x: -x[0])
    if not cands or cands[0][0] < 2:
        return "NOT_IN_PAPERS: the supplied passages do not contain this."
    out, used = [], set()
    for ov, sid, s in cands:
        if ov < 2 or len(out) == 2:
            break
        if s in used:
            continue
        used.add(s)
        out.append(f"{s[:320]} [{sid}]")
    return " ".join(out)


# --------------------------------------------------------------------- handler
def limit_body(kind: str, limit: int, used: int, req: int, wait_s: float) -> dict:
    names = {"rpm": ("requests per minute (RPM)", "requests"), "itpm": ("input tokens per minute (ITPM)", "tokens"), "tpm": ("tokens per minute (TPM)", "tokens"),
             "rpd": ("requests per day (RPD)", "requests"), "tpd": ("tokens per day (TPD)", "tokens")}
    nm, typ = names[kind]
    return {"error": {"message": f"Rate limit reached for model `standin-qwen` in organization `org_standin` service tier `on_demand` on {nm}: Limit {limit}, Used {used}, Requested {req}. Please try again in {wait_s:.2f}s.", "type": typ, "code": "rate_limit_exceeded"}}


def rl_headers(S: State, t: float) -> dict:
    c = S.cfg
    used_tot = sum(n for _, n in S.tot_win)
    reset_tok = (S.tot_win[0][0] + 60 - t) if S.tot_win else 0.5
    return {
        "x-ratelimit-limit-requests": str(c["rpd"]),
        "x-ratelimit-limit-tokens": str(c["tpm"]),
        "x-ratelimit-remaining-requests": str(int(S.rpd_level)),
        "x-ratelimit-remaining-tokens": str(max(0, c["tpm"] - used_tot)),
        "x-ratelimit-reset-requests": fmt_dur((c["rpd"] - S.rpd_level) * 86400 / c["rpd"]),
        "x-ratelimit-reset-tokens": fmt_dur(reset_tok),
    }


def check_limits(S: State, t: float, ptok: int):
    """Return (kind, limit, used, wait_s) when a limit is hit, else None."""
    c = S.cfg
    if S.rpd_level < 1:
        return "rpd", c["rpd"], c["rpd"], (1 - S.rpd_level) * 86400 / c["rpd"]
    if S.tpd_level < ptok:
        return "tpd", c["tpd"], int(c["tpd"] - S.tpd_level), (ptok - S.tpd_level) * 86400 / c["tpd"]
    if len(S.req_win) >= c["rpm"]:
        return "rpm", c["rpm"], len(S.req_win), S.req_win[0][0] + 60 - t
    used_in = sum(n for _, n in S.in_win)
    if used_in + ptok > c["itpm"]:
        acc, wait = used_in, 60.0
        for ts, n in S.in_win:
            acc -= n
            if acc + ptok <= c["itpm"]:
                wait = ts + 60 - t
                break
        return "itpm", c["itpm"], used_in, wait
    used_tot = sum(n for _, n in S.tot_win)
    if used_tot + ptok > c["tpm"]:
        acc, wait = used_tot, 60.0
        for ts, n in S.tot_win:
            acc -= n
            if acc + ptok <= c["tpm"]:
                wait = ts + 60 - t
                break
        return "tpm", c["tpm"], used_tot, wait
    return None


async def chat(request: web.Request) -> web.StreamResponse:
    S: State = request.app["S"]
    c = S.cfg
    body = await request.json()
    t = time.monotonic()
    S.refill(t)
    S.counts["total"] += 1
    msgs = body.get("messages", [])
    system = next((m["content"] for m in msgs if m["role"] == "system"), "")
    user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
    ptok = int((len(system) + len(user)) / c["chars_per_token"]) + 8
    hit = check_limits(S, t, ptok)
    if hit:
        kind, lim, used, wait = hit
        S.counts["r429"] += 1
        h = rl_headers(S, t)
        h["retry-after"] = str(max(1, math.ceil(wait)))
        return web.json_response(limit_body(kind, lim, used, ptok, wait), status=429, headers=h)
    if S.inflight >= c["max_inflight"]:
        S.counts["fail"] += 1
        return web.json_response({"error": {"message": "over capacity", "type": "server_error"}}, status=503, headers=rl_headers(S, t))
    # admitted: charge the windows
    S.req_win.append((t, 1))
    S.in_win.append((t, ptok))
    S.tot_win.append((t, ptok))
    S.rpd_level -= 1
    S.tpd_level -= ptok
    S.inflight += 1
    S.counts["max_inflight"] = max(S.counts["max_inflight"], S.inflight)
    try:
        r = S.rng.random()
        if r < c["fail_rate"]:
            S.counts["fail"] += 1
            await asyncio.sleep(0.05)
            return web.json_response({"error": {"message": "internal error", "type": "server_error"}}, status=503, headers=rl_headers(S, t))
        if r < c["fail_rate"] + c["hang_rate"]:
            S.counts["hang"] += 1
            await asyncio.sleep(120)
            return web.Response(status=504)
        load = 1.0 + c["load_slope"] * max(0, S.inflight - c["load_free"])
        ttft = (c["ttft_a_ms"] + c["ttft_b_ms_per_tok"] * ptok) * S.rng.lognormvariate(-c["ttft_sigma"] ** 2 / 2, c["ttft_sigma"]) * load
        if S.rng.random() < c["tail_prob"]:
            ttft *= c["tail_mult"]
        if S.rng.random() < c["slow_success_rate"]:
            ttft += c["slow_success_extra_ms"]
        text = make_content(S, system, user)
        max_tok = int(body.get("max_tokens") or 1024)
        text = text[: int(max_tok * c["chars_per_token"])]
        ctok = max(1, int(len(text) / c["chars_per_token"]))
        S.tot_win.append((time.monotonic(), ctok))
        S.tpd_level -= ctok
        usage = {"prompt_tokens": ptok, "completion_tokens": ctok, "total_tokens": ptok + ctok, "queue_time": 0.05}
        h = rl_headers(S, t)
        await asyncio.sleep(ttft / 1000)
        if not body.get("stream"):
            await asyncio.sleep(ctok * c["decode_ms_per_tok"] * load / 1000)
            S.counts["ok"] += 1
            return web.json_response({"choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}], "usage": usage, "model": "standin-qwen"}, headers=h)
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", **h})
        await resp.prepare(request)
        words = re.findall(r"\S+\s*", text)
        i = 0
        while i < len(words):
            n = S.rng.randint(1, 3)
            piece = "".join(words[i : i + n])
            i += n
            await asyncio.sleep(max(0.0, (len(piece) / c["chars_per_token"]) * c["decode_ms_per_tok"] * S.rng.lognormvariate(0, c["decode_sigma"]) * load / 1000))
            chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        fin = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "x_groq": {"usage": usage}}
        await resp.write(f"data: {json.dumps(fin)}\n\ndata: [DONE]\n\n".encode())
        await resp.write_eof()
        S.counts["ok"] += 1
        return resp
    finally:
        S.inflight -= 1


async def admin_config(request: web.Request):
    S: State = request.app["S"]
    patch = await request.json()
    unknown = [k for k in patch if k not in S.cfg]
    if unknown:
        return web.json_response({"error": f"unknown keys {unknown}"}, status=400)
    S.cfg.update(patch)
    if "rpd_level" in patch:
        S.rpd_level = float(patch["rpd_level"])
    return web.json_response(S.cfg)


async def admin_special(request: web.Request):
    """Directly set daily bucket levels, e.g. {"rpd_level": 0} to exhaust the daily budget."""
    S: State = request.app["S"]
    patch = await request.json()
    if "rpd_level" in patch:
        S.rpd_level = float(patch["rpd_level"])
    if "tpd_level" in patch:
        S.tpd_level = float(patch["tpd_level"])
    return web.json_response({"rpd_level": S.rpd_level, "tpd_level": S.tpd_level})


async def admin_state(request: web.Request):
    S: State = request.app["S"]
    return web.json_response({"cfg": S.cfg, "counts": S.counts, "inflight": S.inflight, "rpd_level": S.rpd_level, "tpd_level": S.tpd_level})


async def admin_reset(request: web.Request):
    S: State = request.app["S"]
    S.cfg = dict(DEFAULTS) | (await request.json() if request.can_read_body else {})
    S.reset()
    return web.json_response({"ok": True})


def make_app(seed: int | None = None) -> web.Application:
    app = web.Application()
    app["S"] = State(seed)
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_post("/admin/config", admin_config)
    app.router.add_post("/admin/special", admin_special)
    app.router.add_get("/admin/state", admin_state)
    app.router.add_post("/admin/reset", admin_reset)
    return app


# --------------------------------------------------------------------- calibration
def calibrate(path: Path = RAW / "real_profile.jsonl") -> dict:
    """Fit latency parameters from real measurements. Returns the config patch."""
    evs = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    base = [e for e in evs if e["phase"] == "baseline" and e.get("status") == 200 and e.get("ttft_ms") and e.get("usage")]
    xs = [e["usage"]["prompt_tokens"] for e in base]
    ys = [e["ttft_ms"] for e in base]
    mx, my = st.mean(xs), st.mean(ys)
    b = max(0.0, sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs))
    a = my - b * mx
    ratios = [y / max(1.0, a + b * x) for x, y in zip(xs, ys)]
    sigma = st.pstdev([math.log(r) for r in ratios])
    dec = [(e["total_ms"] - e["ttft_ms"]) / max(1, e["usage"]["completion_tokens"] - 1) for e in base if e["usage"].get("completion_tokens", 0) > 20]
    decode = st.median(dec) if dec else DEFAULTS["decode_ms_per_tok"]
    # load: mean total latency of identical tiny requests fired together, by wave size k
    waves: dict[int, list[float]] = {}
    for e in evs:
        if e["phase"] == "conc" and e.get("status") == 200:
            waves.setdefault(int(e["tag"][1:]), []).append(e["total_ms"])
    slope = DEFAULTS["load_slope"]
    if len(waves) >= 3:
        ks = sorted(waves)
        l1 = st.mean(waves[ks[0]]) / (1 + slope * (ks[0] - 1))  # anchor on smallest wave
        # least squares on latency/l1 = 1 + s*(k-1)
        num = sum((st.mean(waves[k]) / l1 - 1) * (k - 1) for k in ks)
        den = sum((k - 1) ** 2 for k in ks)
        slope = max(0.0, num / den)
    return dict(ttft_a_ms=round(a, 1), ttft_b_ms_per_tok=round(b, 4), ttft_sigma=round(min(sigma, 0.6), 3), decode_ms_per_tok=round(decode, 3), load_slope=round(slope, 3),
                _n_baseline=len(base), _waves={k: round(st.mean(v)) for k, v in sorted(waves.items())})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--no-calibrate", action="store_true")
    a = ap.parse_args()
    app = make_app(a.seed)
    if not a.no_calibrate and (RAW / "real_profile.jsonl").exists():
        cal = calibrate()
        app["S"].cfg.update({k: v for k, v in cal.items() if not k.startswith("_")})
        (RAW / "standin_calibration.json").write_text(json.dumps(cal, indent=2), encoding="utf-8")
        print("calibrated from real profile:", cal, flush=True)
    web.run_app(app, host="127.0.0.1", port=a.port, print=lambda *_: None, access_log=None)


if __name__ == "__main__":
    main()
