# Timeline

What changed in each phase and what it achieved. All work was done on 9 Nov 2026. Times are local file timestamps (IST); the evidence file is named in each row.

## Summary: first version vs final

| KPI | First version (`legacy_v1/`) | Final | Evidence |
|---|---|---|---|
| Real steady run: replies within patience | 33.3% (1 of 3) | 100% (12 of 12) | `raw/steady_real.jsonl` |
| Real steady run: total time p50 | 30,000 ms (the timeout) | 1,675 ms | same |
| Real steady run: correct and cited | not measured | 91.7% (11 of 12) | same |
| Answer quality measured on | canned stand-in text; any `[` counted as a citation | the real model; the cited page must contain the fact | `raw/quality_real_*.jsonl` |
| Unanswerable questions refused | 20% of questions, measured on the stand-in | 7 of 7 in all 5 real runs | same |
| Section 4 slow-request trace | function default values (`queue_wait_ms = 850.0`) | measured spans of a real request | `raw/steady_real.jsonl`, `steady_real-006` |
| Table 1 limits | static text copied from documentation | probed: 99 real requests, 21 real 429s | `raw/real_profile.jsonl` |
| Retries per request, cost | hard-coded 0.0 | counted per request; cost at list price | `results.csv` |
| Moshi paper version | v2 (wrong) | v1, as required | `corpus/arxiv_2410.00037v1.pdf` |
| Given up under sustained overload | not measured | 0% | `raw/sustained_overload.jsonl` |
| Retry traffic under a burst, naive vs final | not measured | 239 vs 15 HTTP attempts for the same 13 served | `raw/storm_*.jsonl` |

---

## Phase 0: Audit of the first version (≈ 17:30)

**What:** Read every Markdown file, `task.txt` and the code, and compared report numbers with the code that produced them.

**Found:**
- Section 4's slow request came from `trace_slow_request()` with hard-coded defaults.
- Table 1 was a list of strings.
- Quality came from a stand-in that returned canned paragraphs, and `has_citations = "[" in answer`.
- `retries_per_request = 0.0` and `cost_per_1000 = 0.0` were constants.
- The real steady run timed out 2 of 3 requests at 30 s.
- The Moshi PDF was v2, not the required v1.
- The report claimed a peak queue of 14 "under sustained overload", while that row's own table said 5.

**Decision:** Archive it to `legacy_v1/` and rebuild ([DECISIONS.md D15](devdocs/DECISIONS.md)).

**KPI:** 6 headline figures in the old report traced to constants or to text with no measurement behind it: the trace, Table 1, quality %, retries, cost, peak queue.

## Phase 1: Corpus and retrieval (≈ 17:45–18:30)

**What:**
- Downloaded the three papers at the exact versions and verified each `arXiv:…vN` stamp.
- Wrote generic ingestion for any folder of PDF/txt/md: page-aware chunks, heading merge, running-header removal, bibliography tagging.
- Wrote BM25 search with an idf-weighted coverage score.

**KPIs:**
- 3 documents, 120 pages, loaded in 0.9 s.
- 1,001 / 524 / 325 chunks at 500 / 900 / 1,500 characters.
- Search ≈ 4 ms per query.
- Out-of-scope "carbon footprint of training" scores coverage 0.08; in-scope questions score 0.37–1.0. This made a local refusal threshold possible.

## Phase 2: Real-service profiling (18:30–18:39)

**What:** `profile_real.py` made 99 raw HTTP requests to Groq (`qwen/qwen3.8-27b`): a baseline across 4 prompt sizes, concurrency waves, an RPM burst, an ITPM burst, and a `max_tokens` reservation test.

**KPIs (findings):**

| Finding | Value |
|---|---|
| TTFT p50 / p95 | 354 / 822 ms (17 baseline calls) |
| RPM | 30 confirmed by the 31st request's 429 |
| Hidden cap | **7,000 input tokens/min**, in no header, found in a 429 body |
| Requests per day | 1,000, refilling at 1 per 86.4 s, not at midnight |
| Concurrency | ≥ 12 in flight, all succeeded |
| `max_tokens` | not reserved against TPM |
| Spend | 25,065 tokens |

## Phase 3: Core runtime (≈ 18:40–18:58)

**What:**
- **Limiter:** five limits, whole-request reservation, FIFO with deadline, learning from headers and 429 bodies, shared cooldown.
- **Client:** streaming OpenAI-compatible client; failure classes; full-jitter retries with a retry-after floor, deadline check, retry budget and circuit breaker.
- **Pipeline:** six steps with branch and revise loop, three reply modes, local grounding check.
- **Tracing:** wait / work / retry spans.

**KPI:** first real smoke test, 2 of 2 correct and cited, 0.88–1.08 s end to end, spans summing to the total within 2 ms (`raw/smoke_real.jsonl`).

## Phase 4: Stand-in service and calibration (≈ 18:50–19:00)

**What:**
- An aiohttp server speaking Groq's protocol, headers and 429 wording.
- Faults: 503, hang, slow success, slow tail.
- Limit windows including ITPM; continuously refilling daily buckets.
- Admin API for scenarios.
- Fitted to the Phase 2 data.

**KPIs:**
- Fitted TTFT = 314 ms + 0.115 ms/token, σ = 0.425, decode 2.18 ms/token, load slope 0.081.
- Later check: stand-in steady first-token p50 1,800 ms vs real 1,675 ms (within 8%).

## Phase 5: Evaluation harness (≈ 19:00–19:25)

**What:**
- 21 questions with regex-checked facts, each verified in the PDFs; 7 confirmed absent from all three papers.
- A grader that checks the cited page holds the fact.
- A free offline evidence-recall check.
- A report generator that builds every table from `raw/`.

**KPIs:**
- The offline check caught a regression in my re-ranker: evidence recall 85.7% vs 92.9% for plain BM25.
- Re-ranker reduced to small corrections, plus balanced retrieval for comparative questions: back to 92.9% (a tie, not a win).
- Diagnosis: q14 (two papers) was missing one paper from the retrieved set before balanced retrieval.

## Phase 6: First measurement round (19:03–19:50)

**What:** Five stand-in load shapes; real lean-tier quality for settings A, B, C, F; first real steady and burst runs.

**KPIs:**

| Run | Result |
|---|---|
| Stand-in, all five shapes | 97.5–100% replied within 15 s; 1–2 given up in burst and overload |
| Table 3, lean | 57.1–61.9% correct and cited, 7/7 refused in every setting |
| Setting F (500 × 3) | same accuracy as A at 543 vs 928 tokens per answer |
| Real steady, first run | 12/12 replied, **8/12 correct and cited**; **4 of 12 correct drafts rejected by the model verifier** and replaced with quoted passages under a misleading "service is busy" header |

The first-run steady numbers are from the console log of this session. The raw file was overwritten by the Phase 7 rerun.

## Phase 7: Fixes and reruns (19:52–20:33)

| Fix | Before | After | Evidence |
|---|---|---|---|
| Verifier sees the drafter's exact passages and judges meaning, not wording | steady_real 8/12 correct; 4/12 replaced by passages | **11/12 correct; 0 replaced** | `raw/steady_real.jsonl` |
| Honest header when checks fail ("could not confirm a summary" instead of "service is busy") | misleading | accurate | `pipeline._passages_from_hits` |
| Cooldown and connection waits honour the request deadline | bounded-retry storm: **95% given up** (38/40) | **0% given up** | `raw/storm_bounded_retry.jsonl` |
| Same fix, stand-in load shapes | burst 5.0%, overload 1.7% given up | **0% in all five shapes** | `raw/burst.jsonl`, `raw/sustained_overload.jsonl` |
| JSON-mode fallback after Groq's "Failed to generate JSON" | q13 (full A) fell to passages | retried without JSON mode (added after the runs; not yet exercised) | `pipeline._llm_call` |

Measured along the way:
- Full tier with the verifier, setting A: 52.4% correct vs 57.1% lean, at 1.66× the tokens and 2.4× the first-token time. **The verifier did not raise accuracy.**
- The real daily token limit was hit during `burst_real` (20:07). The 429 read "Limit 200000, Used 199809, Requested 827. Please try again in 4m34.752s", which confirmed continuous refill. 8 of 10 requests were served in budget mode with an ETA in about 3 ms.

## Phase 8: Write-up and reporting (20:20–20:40)

**What:** `SUBMISSION.md` with every heading and table from the brief, plus generated `tables.md`, `tables.csv` and `results.csv`. `results.csv` has one row per request: 361 rows across 16 runs.

**KPIs:**
- Every number in the tables recomputes from `raw/` with one command (`python -m relay.report`).
- All 10 `wrong` rows in the real quality runs were read by hand. None states a false fact; all are omissions or over-refusals.

## Phase 9: Packaging and documentation

**What:**
- `README.md` rewritten.
- `devdocs/` (HLD, Architecture, LLD, Data model, Rate limiting, Stand-in, Evaluation, Operations, Decisions, Known issues).
- `requirements.txt`, `.env.example`, this timeline.
- Copy of the deliverable in `sol/`.

Small corrections found while documenting:
- The `relay.ask` CLI defaulted to 900-char chunks and 4 pieces instead of the chosen 500 × 6. Fixed.
- The model sometimes echoed an `[ANSWER]` tag. Now stripped.
- Two config comments described the wrong formula. Corrected.

**KPI:** the `sol/` copy runs on its own: the offline check, stand-in smoke test and report regenerate identical tables from inside `sol/`.
