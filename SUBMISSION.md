# The Relay

Groq free tier, `qwen/qwen3.8-27b`. All numbers are recomputed from `raw/` by `python -m relay.report`; `results.csv` has one row per request.

## 1 What I built and why

- **Pipeline** over the three papers (arXiv 1706.03762v7, 2410.00037v1, 2507.07955v2):
    1. **Understand** (LLM): rewrite the question as a search query.
    2. **Retrieve** (tool): BM25 over 500-character, page-tagged chunks.
    3. **Branch**: weak evidence → refuse without calling the model.
    4. **Draft** (LLM): answer citing `[S#]`, or reply `NOT_IN_PAPERS`.
    5. **Check** (loop): every id exists and every number is in the cited text, then an LLM verifier; one revision, else quoted passages.
    6. **Deliver** with `[Title, p.N]` citations.
- **Decision that shaped it most:** only checked text is released, and each request's mode is chosen *before* spending quota, from the predicted wait for its whole plan:
    - ≤ 5.25 s → **full** (3 calls, ~2,000 tokens)
    - ≤ 10.5 s → **lean** (1 call, local check only, ~550 tokens)
    - otherwise → **cited quotes** (0 tokens, ~3 ms)
- **Cost:** first token = total time, and under overload most people get quotes, not a summary.

## 2 The real service and my stand-in

### Table 1: Limits you found in the real service

| Limit | Value you found | How you found it | Confidence |
|---|---|---|---|
| Requests per minute | 30, sliding 60 s | 45 tiny requests: 30 OK, the 31st got 429 "(RPM): Limit 30" | high |
| Tokens per minute | 8,000 total, **plus 7,000 input tokens/min (ITPM), in no header** | `x-ratelimit-limit-tokens`; the 4th 1,930-token prompt in 1 s got 429 "(ITPM): Limit 7000" | high |
| Requests per day | 1,000; refills 1 per 86.4 s, not at midnight | `x-ratelimit-limit-requests`; `reset-requests` grew 86.4 s per request | high (value), medium (refill) |
| Tokens per day | 200,000; refills 2.31 per s | Hit for real: "(TPD): Limit 200000, Used 199809, Requested 827, try again in 4m34.752s" = excess × 86400 / limit | high |
| Requests in flight at once | ≥ 12 (ceiling not found) | Waves of 2/4/8/12: all returned 200 | medium |
| What it returns when a limit is hit | HTTP 429; JSON naming the limit with Limit/Used/Requested; `retry-after` (s) | 21 real 429s in `raw/` | high |
| Time until it recovers | RPM ~2 s; ITPM 4 s; daily = excess ÷ refill rate | `retry-after`; polling every 2 s; header series | medium |

**Stand-in** (`relay/standin.py`):
- **Behaviour:** same protocol as Groq (SSE streaming, `x-ratelimit-*` headers, 429 wording, the same 5 limits). Faults: 3% slow (×6), 2% HTTP 503, 1% hang, 2% slow success (+6 s).
- **Calibration** (17 real calls and the concurrency waves): TTFT = (314 ms + 0.115 ms × prompt tokens) × lognormal(σ 0.425) × (1 + 0.081 × extra in-flight requests); decode 2.18 ms/token. Steady first-token p50: 1,800 ms stand-in vs 1,675 ms real.
- **Differs most:** its answers are extracted sentences, not generated, so quality is measured on the real model only. It also slows linearly with load, while Groq was faster at 12 in flight than at 8.

## 3 Results

Percentiles are p50 / p95 / p99 by linear interpolation; every request got a reply, so n = Requests sent. First token = total time. `*_real` rows are Groq; the rest are the stand-in.

### Table 2a: Latency, one row per load scenario

| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |
|---|---|---|---|---|---|---|
| steady_real | real | 12 | 223 | 3.3 | 1675 / 4693 / 6317 | 1675 / 4693 / 6317 |
| burst_real | real | 10 | 3 | 200.0 | 3 / 363 / 425 | 3 / 363 / 425 |
| steady | stand-in | 10 | 183 | 3.3 | 1800 / 2866 / 3058 | 1800 / 2866 / 3058 |
| burst | stand-in | 40 | 8 | 300.0 | 3 / 1319 / 1833 | 3 / 1319 / 1833 |
| sustained_overload | stand-in | 60 | 122 | 30.5 | 4 / 6100 / 7575 | 4 / 6100 / 7575 |
| rpm_halved | stand-in | 20 | 95 | 12.6 | 3 / 2361 / 3370 | 3 / 2361 / 3370 |
| daily_exhausted | stand-in | 20 | 57 | 21.1 | 3 / 63 / 949 | 3 / 63 / 949 |

### Table 2b: Outcomes, same rows

| Scenario | Succeeded % | Rate-limited % | Timed out or given up % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers |
|---|---|---|---|---|---|---|---|
| steady_real | 100.0 | 0.0 | 0.0 | 0.17 | 1 | 2073 | $2.04 |
| burst_real | 100.0 | 10.0 | 0.0 | 0.00 | 1 | 41 | $0.06 |
| steady | 100.0 | 30.0 | 0.0 | 0.20 | 1 | 1988 | $1.98 |
| burst | 100.0 | 0.0 | 0.0 | 0.00 | 1 | 175 | $0.17 |
| sustained_overload | 100.0 | 8.3 | 0.0 | 0.13 | 3 | 225 | $0.24 |
| rpm_halved | 100.0 | 0.0 | 0.0 | 0.00 | 1 | 340 | $0.36 |
| daily_exhausted | 100.0 | 0.0 | 0.0 | 0.00 | 1 | 120 | $0.12 |

- **Succeeded includes cited-quote replies.** sustained_overload: 5 full, 8 lean, 47 quotes. burst: 20 of 40 served from cache or a shared in-flight answer. burst_real hit the real daily token limit, so 8 of 10 got budget replies.
- **Rate-limited** = received a 429 and recovered. **Cost** is at list price ($0.77/M input, $4.00/M output); the free tier bills $0.
- **Answer quality** (21 questions I wrote: 14 answerable, 7 not in the papers; real model; Table 3): 11/12 correct and cited under real steady load, and 7/7 unanswerable refused in all five quality runs.
- **Number I could not have guessed:** the LLM verifier *lowered* accuracy, 52.4% vs 57.1% without it, at 1.66× the tokens and 2.4× the first-token time.

## 4 Where the time goes

Slowest real request, `steady_real-006` (spans in ms, from `raw/steady_real.jsonl`):

> admission 16 (wait) → understand 455 → retrieve 2 → draft attempt 1, stalled and cut at 5 s, 5,080 (retry) → back-off 313 (retry) → draft 482 → verify 374 = **6,723**

- **Split:** waiting 16, working 1,312, **retrying 5,393 (80%)**. All of it came **before the first token**, released at 6,723 ms once verify passed.
- **How I know:** a span wraps every attempt and every sleep, and the spans sum to the measured total within 2 ms.

## 5 When a limit is hit

- **Per-minute:** a request reserves its whole plan up front.
    - **Delayed** if it fits within 5.25 s.
    - **Weaker** otherwise: lean mode (no rewrite or verifier, 3 passages, marked "Quick mode").
    - **Cited quotes at once** if even lean would wait over 10.5 s.
- **After a 429:** all calls pause for `retry-after`; a call whose deadline ends first falls back at once. Repeated questions share one answer.
- **Per-day:** if even lean no longer fits, nothing is queued. The reply is cited quotes plus an ETA, in ~3 ms.
- **Dropped:** only requests stalled past the 15 s patience; 0 in the final runs.
- **Why:** a cited quote now beats a 15 s wait or an unchecked summary.

## Retrieval: quality versus cost

### Table 3: Retrieval settings (real model, 21 questions)

| Setting (piece size, pieces fetched, re-rank yes or no) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |
|---|---|---|---|---|---|---|---|
| 500 chars, 6 pieces, no | 21 | 57.1 | 9.5 | 33.3 | 928 | 416 / 606 | 416 / 606 |
| 500 chars, 3 pieces, no | 21 | 57.1 | 9.5 | 33.3 | 543 | 367 / 749 | 367 / 749 |
| 900 chars, 4 pieces, yes | 21 | 57.1 | 9.5 | 33.3 | 1045 | 396 / 654 | 396 / 654 |
| 1500 chars, 3 pieces, yes | 21 | 61.9 | 4.8 | 33.3 | 1221 | 408 / 918 | 408 / 918 |
| 500 chars, 6 pieces, no, + LLM verifier | 21 | 52.4 | 14.3 | 33.3 | 1540 | 1004 / 1699 | 1004 / 1699 |

Rows 1–4 use lean mode. Times exclude admission waits, because these runs were paced.

- **Chosen: 500 chars, 6 pieces, no re-rank.** All settings are within one question of each other (noise at n = 21), and 6 pieces retrieve more evidence (13/14 vs 12/14 for 500 × 3).
- **Re-ranking did not pay:** my first version cut evidence recall from 92.9% to 85.7%; trimmed back, it only tied plain BM25.
- **Near the limit:** lean mode with 500 × 3 uses 543 tokens vs 1,540 for full, and scored higher (57.1% vs 52.4%).

## How this library will mislead a naive retriever

1. **Bibliographies outrank the paper.** "Who wrote Attention Is All You Need?" returns reference lists in Moshi (p.51) and Dynamic Chunking (p.29). Fix: down-weight bibliography chunks.
2. **One quantity, many values.** "What is the latency of the model?" ranks Moshi's ablation values (640/240/80 ms, p.26) above the headline 160/200 ms (p.1). Fix: the rewrite names the paper, and the number check ties each number to its passage.
3. **Close but unanswered.** "What BLEU score does the Transformer get on English-to-Japanese?" returns the English-German/French scores (28.4 / 41.8). Fix: the `NOT_IN_PAPERS` contract; all 7 such questions were refused in every run.

## 6 Retries

- **Policy:** retry only 429, 5xx, timeouts and stalls, at most 3 attempts. Full-jitter back-off (0.5 s base, 8 s cap), never shorter than `retry-after`, skipped if it would pass the deadline. Retries are capped at 20% of calls, a 429 pauses all calls, and 6 straight failures open a circuit breaker.
- **When retrying made it worse** (stand-in, 40 requests in 6 s, admission control off; only the retry policy differs):

| Policy | HTTP attempts | 429s received | Calls served | Total p50 (ms) |
|---|---|---|---|---|
| Naive: 6 tries, fixed 1–2 s, ignores `retry-after` | 239 | 225 | 13 | 9094 |
| Bounded (mine) | 15 | 1 | 13 | 4 |

- **My own mistake:** the first bounded version paused with no deadline, and 95% of users gave up. A deadline-aware pause brought that to 0%.

## 7 What the person sees

The real reply sent in ~3 ms, without queueing, when Groq returned the daily-token 429 (`burst_real-002`):

> Today's allowance for written answers is used up. The daily allowance refills gradually; roughly 5 min until a full answer is possible. Here are the most relevant passages from the papers, quoted as written:
> • [Moshi: a speech-text foundation model for real-time dialogue, p.8] … our 7B-parameter Helium language model …

The ETA is the provider's deficit ÷ refill rate. If nothing in the papers is relevant, the reply says so.

## 8 What I'm unsure about, what I left out, what I learned

- **Least certain assumption:** that cited quotes count as a served reply. If users see them as failure, overload success drops from 100% to 21.7% (13/60 written answers), and the fix is a paid tier or smaller prompts, not cleverer queueing.
- **Not built:**
    1. Embeddings or a vector DB: BM25 found the evidence for 13/14 answerable questions in ~4 ms.
    2. A shared limiter store: two processes on one key would each spend the whole quota.
- **New to me:** Groq's daily limits are buckets that refill continuously (one request per 86.4 s, 2.3 tokens per second). So the honest message is "about N minutes" (deficit ÷ refill rate), not "come back tomorrow". The 429's "try again in 4m34s" matched that formula to the second.

## Your feedback on Vaani

- **Stood out:** concrete claims, such as ~450 ms p90 latency and "zero dead air" small talk.
- **Confused me:** without JavaScript the counters read "0M+", "0.0%" and "<0ms", and "p90 latency" doesn't say what it measures.
- **Would change:** publish how latency is measured, with a test anyone can reproduce.

## How I worked with the assistant

I used Kiro (an agentic coding assistant) in two sessions. The full, unedited conversation is in chat_history/. The first session produced the version now in legacy_v1/. The second rebuilt it; everything in this report comes from the second.

**The three prompts that mattered most**

1. *"Get context from the md files and task.txt, figure out the existing system and the required task, and improve the system, but stick to the format in task.txt."* This made the assistant audit the existing system before building, and pick up the directions I had written into the project's Markdown files.

2. *"Make sure the system is generalised for the real-world case."* This turned a three-paper demo into code with no paper-specific logic: it works with any document folder and any OpenAI-compatible provider, learns limits from response headers and 429 bodies, and can persist daily counters across restarts.

3. *"Fix the issue fast and complete the project."* This ended open-ended investigation and produced the verifier fix (real steady run: 8/12 → 11/12 correct and cited) and the final runs.

- **Got wrong, and how I caught it:** the verifier rejected correct answers and sent users quotes under an untrue "service is busy" header; it passed on the stand-in, and I caught it by reading the 12 real steady answers one by one.
- **Overruled:** the assistant proposed sentence-transformers and ChromaDB; I kept BM25, which found the evidence for 13/14 answerable questions in ~4 ms with no model download.
- **Distinctly mine:** the direction and acceptance criteria (measure everything, follow the brief's format, generalise beyond these papers), and the decision to discard the first version's numbers.

