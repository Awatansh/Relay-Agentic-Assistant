# The Stand-in Model Service

`relay/standin.py` is a local HTTP server that behaves like the Groq chat-completions endpoint, so the pipeline can be run under loads the free tier cannot take. The client code is identical for both: only the base URL changes.

## 1. Interface

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI-compatible; streaming (SSE) and non-streaming |
| `POST /admin/config` | patch any setting, e.g. `{"rpm": 15, "fail_rate": 0.05}`; unknown keys are rejected |
| `POST /admin/special` | set the daily buckets directly, e.g. `{"rpd_level": 6}` |
| `GET /admin/state` | current settings, counters, in-flight count, daily levels |
| `POST /admin/reset` | restore defaults and empty all windows |

Start it: `python -m relay.standin --port 8765 --seed 11`. On start it fits itself to `raw/real_profile.jsonl` if that file exists. `runner.py` starts and stops it automatically.

Responses copy Groq's surface:
- the `x-ratelimit-limit/remaining/reset-*` headers, with the same meaning (requests = per day, tokens = per minute)
- 429 bodies with Groq's exact wording, `retry-after` in whole seconds, and usage in `x_groq.usage` on the final stream chunk

## 2. Behaviour

| Behaviour | Model |
|---|---|
| Time to first token | `(a + b × prompt_tokens) × lognormal(−σ²/2, σ) × load` |
| Load slow-down | `load = 1 + slope × max(0, in_flight − 1)` |
| Decode | `decode_ms_per_tok × lognormal(0, 0.25) × load` per streamed piece of 1–3 words |
| Unexplained slow calls | 3% of calls: TTFT × 6 |
| Slow success | 2%: +6 s, then a normal reply |
| Failure | 2%: HTTP 503 after 50 ms |
| Hang | 1%: no reply for 120 s (the client must time out) |
| Over capacity | > 64 in flight: HTTP 503 |
| Limits, checked in order | RPD, TPD, RPM (30), ITPM (7,000), TPM (8,000) |
| Daily buckets | refill continuously, like Groq |
| Prompt tokens | characters / 3.8 + 8 |

Rates shown are the `FAULTS` values the load scenarios use. The defaults without a scenario patch are 0% faults and 3% tail.

### Content

The stand-in recognises the pipeline step from a `[[TASK]]` tag in the system prompt:

| Step | Reply |
|---|---|
| `[[UNDERSTAND]]` | JSON with the question's keywords as the search query |
| `[[VERIFY]]` | JSON `PASS`, or `REVISE` 15% of the time |
| `[[ANSWER]]`, `[[REVISE]]` | the 2 sentences from the supplied passages with the most query-word overlap, each with its `[S#]`; `NOT_IN_PAPERS` if no sentence shares ≥ 2 words |

It never sees the papers except through the passages in the prompt, so retrieval quality still matters. It is **not** a language model, and no answer-quality figure in the report comes from it.

## 3. Calibration

`calibrate()` fits the latency parameters from the 17 successful baseline calls and the concurrency waves in `raw/real_profile.jsonl`:

| Parameter | Method | Value |
|---|---|---|
| `ttft_a_ms`, `ttft_b_ms_per_tok` | least squares of TTFT on prompt tokens (60–2,413 tokens) | 314.3 ms, 0.1148 ms/token |
| `ttft_sigma` | standard deviation of log(observed / fitted), capped at 0.6 | 0.425 |
| `decode_ms_per_tok` | median of (total − TTFT) / (completion tokens − 1) where completion > 20 | 2.183 |
| `load_slope` | least squares of mean wave latency / smallest-wave latency against (k − 1) | 0.081 |

Result written to `raw/standin_calibration.json` and applied on every `runner` reset.

### Check against the real service

| Run | Requests | First token p50 / p95 (ms) |
|---|---|---|
| steady_real (Groq) | 12 | 1,675 / 4,693 |
| steady (stand-in, same 3 req/min shape) | 10 | 1,800 / 2,866 |

The medians agree within 8%. The real p95 is higher because of the stalls (5 s timeouts) that hit 2 of 12 real requests. The stand-in's 1% hang rate produced fewer of them in a run of 10.

## 4. Where it differs most from Groq

1. **Content.** Extracted sentences, not generated text. Quality is measured on the real model only.
2. **Latency under concurrency.** Groq's mean latency at 12 in flight (375 ms) was lower than at 8 (426 ms), which suggests server-side batching. The stand-in grows linearly with in-flight count.
3. **Stalls.** Groq's stalls look like a silent connection for > 5 s; the stand-in's "hang" holds for 120 s. Both are cut by the client's 5 s timeout, so the effect on the pipeline is the same.
4. **Accounting.** The stand-in charges completion tokens to the minute window when the request starts; Groq appears to charge them as they are produced.
