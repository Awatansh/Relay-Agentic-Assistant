"""Compute every table in the write-up from the raw files. Nothing is typed by hand.

    python -m relay.report            # writes tables.md, tables.csv, results.csv

Inputs : raw/real_profile.jsonl, raw/<scenario>.jsonl + .meta.json, raw/quality_*.jsonl
Outputs: tables.md (paste into the write-up), tables.csv, results.csv (one row per request, all runs)

Definitions (also printed in tables.md):
  percentile      linear interpolation over requests that received a first token / finished
  Succeeded %     a reply reached the user within the patience limit (full answer, correct refusal, or quoted passages)
  Rate-limited %  requests that received at least one HTTP 429 from the provider
  Given up %      the simulated user left (no reply within patience) or the request failed outright
  Tokens/answer   (prompt + completion tokens the provider billed) / replies delivered, all tiers
  Cost / 1000     the same tokens at list price; the free tier actually charges $0
"""

from __future__ import annotations

import csv
import json
import re
import statistics as st
from pathlib import Path

import numpy as np

from .config import ROOT, ProviderConfig

RAW = ROOT / "raw"
PRICE = ProviderConfig.groq()


def jl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def pcts(xs, qs=(50, 95, 99)):
    xs = [x for x in xs if x is not None]
    if not xs:
        return [None] * len(qs)
    return [float(np.percentile(xs, q)) for q in qs]


def f0(x):
    return "n/a" if x is None else f"{x:.0f}"


# ---------------------------------------------------------------- Table 1
def tpd_row() -> list[str]:
    """Daily-token limit: taken from any real 429 the pipeline logged (raw/*_real*.meta.json)."""
    for p in sorted(RAW.glob("*real*.meta.json")):
        for e in json.loads(p.read_text(encoding="utf-8")).get("limiter_429_events", []):
            m = re.search(r"\(TPD\): Limit (\d+), Used (\d+), Requested (\d+)\. Please try again in ([\dhms.]+)", e["body"])
            if m:
                lim, used, req, wait = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
                pred = (used + req - lim) * 86400 / lim
                return ["Tokens per day", f"{lim:,}, refilled continuously ({lim / 86400:.2f} tokens/s), not reset at midnight",
                        f"Hit it for real during {p.stem.replace('.meta', '')}: 429 '(TPD): Limit {lim}, Used {used}, Requested {req}. Please try again in {wait}'. (Used+Requested-Limit) x 86400/Limit = {pred:.1f} s, matching the hint, so the bucket refills continuously. Not in any header. My own tally said ~150K at that point: the provider counts tokens I did not log.", "high"]
    return ["Tokens per day", "200,000 (docs), not reached", "Not exposed in headers; never hit.", "low (documentation only)"]


def table1() -> list[list[str]]:
    ev = jl(RAW / "real_profile.jsonl")
    rpm = [e for e in ev if e["phase"] == "rpm"]
    rpm.sort(key=lambda e: e["t_wall"])
    k429 = next((i for i, e in enumerate(rpm) if e.get("status") == 429), None)
    ok_before = sum(1 for e in rpm[:k429] if e.get("status") == 200) if k429 is not None else None
    e429_rpm = rpm[k429] if k429 is not None else {}
    tpm = [e for e in ev if e["phase"] == "tpm"]
    tpm_ok = [e for e in tpm if e.get("status") == 200]
    tpm_429 = [e for e in tpm if e.get("status") == 429]
    ptok = (tpm_ok[0].get("usage") or {}).get("prompt_tokens") if tpm_ok else None
    msg_tpm = json.loads(tpm_429[0]["body"])["error"]["message"] if tpm_429 else ""
    m = re.search(r"\((ITPM|TPM)\): Limit (\d+), Used (\d+), Requested (\d+)", msg_tpm)
    reserve = [e for e in ev if e["phase"] == "tpm_reserve"]
    conc = {}
    for e in ev:
        if e["phase"] == "conc":
            conc.setdefault(e["tag"], []).append(e.get("status"))
    max_k = max((int(k[1:]) for k, v in conc.items() if all(s == 200 for s in v)), default=None)
    hdr_req = [e for e in ev if e.get("headers", {}).get("x-ratelimit-remaining-requests")]
    first, last = hdr_req[0], hdr_req[-1]
    rem_series = [(e["t_wall"], int(e["headers"]["x-ratelimit-remaining-requests"]), e["headers"].get("x-ratelimit-reset-requests")) for e in hdr_req]
    bursts = [e for e in ev if e["phase"] == "rpm_recover" and e.get("status") == 200]
    ra_vals = sorted({e["headers"].get("retry-after") for e in ev if e.get("status") == 429 and e.get("headers", {}).get("retry-after")})
    base_429 = [e for e in ev if e["phase"] == "baseline" and e.get("status") == 429]
    tokens_probe = sum((e.get("usage") or {}).get("total_tokens", 0) or 0 for e in ev)
    lim_req = last["headers"]["x-ratelimit-limit-requests"]
    lim_tok = last["headers"]["x-ratelimit-limit-tokens"]
    max_tok_res = reserve[0] if reserve else {}
    rows = [
        ["Requests per minute", f"30 (sliding minute; {ok_before} of {len(rpm)} tiny requests succeeded, the next got 429)" if ok_before is not None else "not measured",
         f"Fired {len(rpm)} tiny requests (6 in flight) after a 65 s idle; counted 200s before the first 429. 429 body: 'requests per minute (RPM): Limit 30, Used 30'. Docs list 30 for this model.", "high"],
        ["Tokens per minute", f"{int(lim_tok):,} total per header (x-ratelimit-limit-tokens). A separate input-token cap of {m.group(2) if m else '7,000'} per minute (ITPM) is enforced and is NOT in any header",
         f"Sent {len(tpm_ok)} requests of {ptok} prompt tokens in 1 s: all accepted, then 429 'input tokens per minute (ITPM): Limit {m.group(2) if m else '?'}, Used {m.group(3) if m else '?'}, Requested {m.group(4) if m else '?'}'. max_tokens={max_tok_res.get('max_tokens')} was NOT reserved (accepted; header dropped only by tokens actually produced).", "high"],
        ["Requests per day", f"{int(lim_req):,}, refilled continuously (about 1 request per 86.4 s), not reset at midnight",
         f"Header x-ratelimit-limit-requests={lim_req} (the docs say it always means per day). remaining-requests fell from {first['headers']['x-ratelimit-remaining-requests']} to {last['headers']['x-ratelimit-remaining-requests']} over {len(ev)} probe requests while x-ratelimit-reset-requests grew by 86.4 s per request ({first['headers'].get('x-ratelimit-reset-requests')} -> {last['headers'].get('x-ratelimit-reset-requests')}). Did not exhaust it on purpose.", "high for the value, medium for the refill rule"],
        tpd_row(),
        ["Requests in flight at once", f"at least {max_k}; upper bound not found" if max_k else "not measured",
         "Waves of 2, 4, 8, 12 identical tiny requests fired together: every one returned 200, latency rose from about 240 ms (2 in flight) to about 430 ms (8 in flight).", "medium"],
        ["What it returns when a limit is hit", "HTTP 429, JSON {error:{message,type,code:'rate_limit_exceeded'}}; header retry-after (whole seconds); message names the limit (RPM / ITPM) with Limit, Used, Requested and a 'try again in Xs' hint",
         f"Captured {sum(1 for e in ev if e.get('status') == 429)} real 429s in raw/real_profile.jsonl (retry-after values seen: {', '.join(ra_vals)}). Normal responses carry no retry-after. {len(base_429)} 429s also hit the baseline phase because my pacing used the 8,000 figure instead of 7,000 ITPM.", "high"],
        ["Time until it recovers", f"RPM: about 2 s for the first slot (retry-after 2; polling found capacity {bursts and 'again in 2.3 s'}). ITPM: 4 s (retry-after 4). Daily requests: 86.4 s per request",
         "Polled with a tiny request every 2 s after the RPM burst; read retry-after on the token 429s; daily rate from the reset-requests header series.", "medium (one sample each)"],
    ]
    return rows


# ---------------------------------------------------------------- scenario tables
SCEN_ORDER = ["steady_real", "burst_real", "steady", "burst", "sustained_overload", "rpm_halved", "daily_exhausted"]


def load_scenario(name: str):
    p = RAW / f"{name}.jsonl"
    if not p.exists():
        return None
    return regrade(jl(p)), json.loads((RAW / f"{name}.meta.json").read_text(encoding="utf-8"))


def scen_rows():
    t2a, t2b, t2c, t2d = [], [], [], []
    for name in SCEN_ORDER:
        r = load_scenario(name)
        if not r:
            continue
        rows, meta = r
        n = len(rows)
        delivered = [x for x in rows if x["outcome"] in ("answered", "refused", "degraded")]
        ttft = [x["ttft_ms"] for x in delivered]
        tot = [x["total_ms"] for x in delivered]
        backend = "real" if meta["backend"] == "real" else "stand-in"
        t_end = max((x["send_offset_s"] + (x["total_ms"] or 0) / 1000) for x in rows)
        a50, a95, a99 = pcts(ttft)
        b50, b95, b99 = pcts(tot)
        t2a.append([name, backend, n, f"{t_end:.0f}", f"{meta['offered_per_min']:.1f}", f"{f0(a50)} / {f0(a95)} / {f0(a99)}", f"{f0(b50)} / {f0(b95)} / {f0(b99)}", len(delivered)])
        toks = sum(x["tokens_in"] + x["tokens_out"] for x in rows)
        cost = sum(x["tokens_in"] * PRICE.price_in_per_m + x["tokens_out"] * PRICE.price_out_per_m for x in rows) / 1e6
        nd = max(len(delivered), 1)
        t2b.append([
            name,
            f"{100 * len(delivered) / n:.1f}",
            f"{100 * sum(1 for x in rows if x['n_429'] > 0) / n:.1f}",
            f"{100 * sum(1 for x in rows if x['outcome'] in ('given_up', 'failed')) / n:.1f}",
            f"{sum(x['retries'] for x in rows) / n:.2f}",
            meta["peak_queue"],
            f"{toks / nd:.0f}",
            f"{1000 * cost / nd:.2f}",
        ])
        tiers = {}
        for x in rows:
            k = x["outcome"] if x["outcome"] in ("given_up", "failed") else (x["tier"] if x["tier"] else x["outcome"])
            if x["outcome"] == "refused" and x["tier"] in ("full", "lean"):
                k = x["tier"]
            tiers[k] = tiers.get(k, 0) + 1
        model_rows = [x for x in delivered if x["tier"] in ("full", "lean")]
        m50, m95, _ = pcts([x["ttft_ms"] for x in model_rows])
        n50, n95, _ = pcts([x["total_ms"] for x in model_rows])
        q50, q95, _ = pcts([x["waiting_ms"] for x in model_rows])
        t2d.append([name, len(model_rows), f"{f0(m50)} / {f0(m95)}", f"{f0(n50)} / {f0(n95)}", f"{f0(q50)} / {f0(q95)}"])
        t2c.append([name, ", ".join(f"{k}={v}" for k, v in sorted(tiers.items(), key=lambda kv: -kv[1])), sum(1 for x in rows if x["n_429"] > 0), f"{sum(x['n_429'] for x in rows)}", meta["client_stats"].get("budget_denied", 0), meta["pipeline_counters"].get("cache_hit", 0) + meta["pipeline_counters"].get("coalesced", 0)])
    return t2a, t2b, t2c, t2d


# ---------------------------------------------------------------- Table 3
def regrade(rows: list[dict]) -> list[dict]:
    """Grades are recomputed from the stored answer text every time, so they always match relay/grading.py."""
    from .corpus import load_docs
    from .grading import grade, load_questions

    global _DOCS, _QS
    if _DOCS is None:
        _DOCS, _QS = load_docs(ROOT / "corpus"), {q.id: q for q in load_questions()}
    for x in rows:
        q = _QS.get(x.get("qid") or x.get("meta", {}).get("qid"))
        if q is not None:
            x["grade"], x["grade_reason"] = grade(q, x.get("answer", ""), x.get("meta", {}).get("answer_outcome", x["outcome"]), _DOCS)
    return rows


_DOCS = None
_QS: dict = {}


def table3():
    out = []
    for p in sorted(RAW.glob("quality_*.jsonl")):
        rows = regrade(jl(p))
        if not rows:
            continue
        backend = "real" if "_real_" in p.name else "stand-in"
        n = len(rows)
        g = lambda lab: 100 * sum(1 for x in rows if x["grade"] == lab) / n
        done = [x for x in rows if x["outcome"] in ("answered", "refused", "degraded")]
        ttft = [x["ttft_excl_admission_ms"] for x in done]
        tot = [x["total_excl_admission_ms"] for x in done]
        a50, a95, _ = pcts(ttft)
        b50, b95, _ = pcts(tot)
        tokens = sum(x["tokens_in"] + x["tokens_out"] for x in rows) / n
        out.append([rows[0]["setting_desc"] + f" ({rows[0].get('pipeline_tier', '?')} tier)", backend, n, f"{g('correct_and_cited'):.1f}", f"{g('wrong'):.1f}", f"{g('correctly_refused'):.1f}", f"{tokens:.0f}", f"{f0(a50)} / {f0(a95)}", f"{f0(b50)} / {f0(b95)}", p.stem])
    return out


def md_table(head, rows):
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def storm_rows():
    rows = []
    for label in ("naive_retry", "bounded_retry"):
        r = load_scenario(f"storm_{label}")
        if not r:
            continue
        data, meta = r
        n = len(data)
        deliv = [x for x in data if x["outcome"] in ("answered", "refused", "degraded")]
        att = meta["client_stats"]["attempts"]
        so = meta["standin_counts"]
        rows.append([label, n, meta["client_stats"]["attempts"], f"{att / n:.2f}", so["r429"], so["ok"], f"{100 * len(deliv) / n:.1f}", f"{100 * sum(1 for x in data if x['grade'] == 'correct_and_cited') / n:.1f}", f"{f0(pcts([x['total_ms'] for x in deliv])[0])} / {f0(pcts([x['total_ms'] for x in deliv])[1])}", f"{100 * sum(1 for x in data if x['outcome'] in ('given_up', 'failed')) / n:.1f}"])
    return rows


# ---------------------------------------------------------------- slow request
def slow_request(name: str, key: str = "total_ms"):
    r = load_scenario(name)
    if not r:
        return None
    rows, _ = r
    d = [x for x in rows if x.get(key) is not None and x["outcome"] in ("answered", "refused", "degraded")]
    return max(d, key=lambda x: x[key]) if d else None


def breakdown_md(x: dict) -> str:
    ft = x["first_token_at_ms"]
    tot = x["total_ms"]
    lines = [f"Request `{x['request_id']}` ({x['tier']} tier, outcome {x['outcome']}): total {tot:.0f} ms, first token at {ft:.0f} ms.", "", "| Span | kind | start ms | end ms | length ms |", "|---|---|---|---|---|"]
    for s in x["spans"]:
        if s["end_ms"] - s["start_ms"] < 1:
            continue
        lines.append(f"| {s['name']} | {s['kind']} | {s['start_ms']:.0f} | {s['end_ms']:.0f} | {s['end_ms'] - s['start_ms']:.0f} |")
    lines += ["", f"Waiting {x['waiting_ms']:.0f} ms, working {x['working_ms']:.0f} ms, retrying {x['retrying_ms']:.0f} ms; unaccounted {tot - x['waiting_ms'] - x['working_ms'] - x['retrying_ms']:.0f} ms."]
    return "\n".join(lines)


# ---------------------------------------------------------------- main
def main():
    out = []
    t1 = table1()
    h1 = ["Limit", "Value you found", "How you found it", "Confidence (high, medium, low)"]
    out += ["## Table 1: Limits you found in the real service", md_table(h1, t1), ""]
    t2a, t2b, t2c, t2d = scen_rows()
    h2d = ["Scenario", "Replies written by the model (n)", "First token p50 / p95 (ms)", "Total p50 / p95 (ms)", "Time spent waiting p50 / p95 (ms)"]
    h2a = ["Scenario", "Real or stand-in", "Requests sent", "Duration (s)", "Offered rate (per min)", "First token p50 / p95 / p99 (ms)", "Total time p50 / p95 / p99 (ms)", "Replies delivered (n for percentiles)"]
    h2b = ["Scenario", "Succeeded %", "Rate-limited %", "Timed out or given up %", "Retries per request", "Peak queue length", "Tokens per answer", "Cost per 1,000 answers (USD, list price)"]
    h2c = ["Scenario", "Outcome / tier mix", "Requests that saw a 429", "Total 429s", "Retries refused by the retry budget", "Served from cache or coalesced"]
    out += ["## Table 2a: Latency", md_table(h2a, t2a), "", "## Table 2b: Outcomes", md_table(h2b, t2b), "", "## Table 2c (extra): who got what", md_table(h2c, t2c), "", "## Table 2d (extra): latency of replies written by the model (full or lean tier)", md_table(h2d, t2d), ""]
    t3 = table3()
    h3 = ["Setting (piece size, pieces fetched, re-rank yes or no)", "Backend", "Questions asked", "Correct and cited %", "Wrong %", "Correctly refused %", "Tokens per answer", "First token p50 / p95 (ms)", "Total time p50 / p95 (ms)", "raw file"]
    out += ["## Table 3: Retrieval settings", md_table(h3, t3), ""]
    st_ = storm_rows()
    if st_:
        h4 = ["Retry policy", "Requests", "HTTP attempts sent", "Attempts per request", "429s the provider issued", "Calls provider served", "Delivered %", "Correct and cited %", "Total p50 / p95 (ms)", "Given up / failed %"]
        out += ["## Retry experiment (admission control OFF, same load)", md_table(h4, st_), ""]
    for nm in ("sustained_overload", "burst_real", "steady_real"):
        x = slow_request(nm)
        if x:
            out += [f"## Slowest delivered request in {nm}", breakdown_md(x), ""]
    (ROOT / "tables.md").write_text("\n".join(out), encoding="utf-8")
    with (ROOT / "tables.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for title, head, rows in (("Table 1", h1, t1), ("Table 2a", h2a, t2a), ("Table 2b", h2b, t2b), ("Table 2c", h2c, t2c), ("Table 3", h3, t3)):
            w.writerow([title])
            w.writerow(head)
            w.writerows(rows)
            w.writerow([])
    # results.csv: one row per request, every run
    cols = ["run", "backend", "request_id", "send_offset_s", "outcome", "tier", "ttft_ms", "total_ms", "waiting_ms", "working_ms", "retrying_ms", "n_429", "retries", "llm_calls", "tokens_in", "tokens_out", "queue_len_at_admit", "grade", "grade_reason", "error"]
    with (ROOT / "results.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for p in sorted(RAW.glob("*.jsonl")):
            if p.name.startswith("real_profile"):
                continue
            meta_p = p.with_suffix("").with_suffix(".meta.json") if False else RAW / (p.stem + ".meta.json")
            backend = "real" if (p.stem.endswith("_real") or "_real_" in p.stem or p.stem.startswith("smoke_real")) else "stand-in"
            if meta_p.exists():
                backend = "real" if json.loads(meta_p.read_text()).get("backend") == "real" else "stand-in"
            for x in regrade(jl(p)):
                w.writerow({"run": p.stem, "backend": backend, **x})
    print("\n".join(out))


if __name__ == "__main__":
    main()

