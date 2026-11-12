# Rate Limiting and Retries

What the provider enforces, how the system models it, and the retry policy, with the measurements behind each choice.

## 1. What Groq enforces (measured, `qwen/qwen3.8-27b`, free tier)

| Limit | Value | Visible in headers? | Window | Evidence |
|---|---|---|---|---|
| Requests per minute | 30 | no | sliding 60 s | 30 of 45 burst requests succeeded, the 31st got 429 `(RPM): Limit 30, Used 30` |
| Tokens per minute | 8,000 | yes: `x-ratelimit-limit-tokens` | sliding 60 s | header on every response |
| **Input tokens per minute** | **7,000** | **no** | sliding 60 s | 4th 1,930-token prompt in 1 s got 429 `(ITPM): Limit 7000, Used 5534, Requested 1930` |
| Requests per day | 1,000 | yes: `x-ratelimit-limit-requests` | continuous refill, 1 per 86.4 s | `x-ratelimit-reset-requests` grew by 86.4 s per request |
| Tokens per day | 200,000 | no | continuous refill, 2.31 per s | real 429 `(TPD): Limit 200000, Used 199809, Requested 827. Please try again in 4m34.752s` matches (199809 + 827 − 200000) × 86400 / 200000 = 274.8 s |
| Concurrent requests | ≥ 12 | no | — | waves of 2/4/8/12 all succeeded |

Other observed behaviour:
- `max_tokens` is **not** reserved against TPM. A `max_tokens=7500` request was accepted with 7,930 tokens remaining.
- `retry-after` appears only on 429s, in whole seconds: 1–6 s for minute limits, 275 s for the daily-token limit.
- About 1 in 20 calls (2 of 38 in the final steady run) stalls with no bytes for over 5 s, then succeeds on retry.
- JSON mode can fail server-side with "Failed to generate JSON" (seen once in 70 calls).

## 2. How the limiter models it

```
            ┌────────────── per request ──────────────┐   ┌──── per HTTP call ────┐
question -> plan(tier) -> predicted_wait -> admit(...) -> slot(deadline) -> HTTP -> settle(actual)
                              │                │            │
                              │                │            ├ semaphore: 8 in flight
                              │                │            └ cooldown after a 429
                              │                ├ FIFO queue with a deadline
                              │                └ charges RPM / ITPM / TPM windows and daily counters
                              └ chooses full / lean / passages / budget
```

**Whole-request reservation.** A full-tier request reserves 3 calls and its estimated input and output tokens at admission. After it finishes, `settle` charges or refunds the difference. Without this, a burst would admit many understand calls and then starve the draft calls behind them, leaving every request half done.

**Safety margin.** Every cap is multiplied by 0.9, to absorb estimation error and other clients on the same key.

**Learning.**
- Every response's `x-ratelimit-*` headers update TPM, RPD and the daily usage seen by the provider.
- Every 429's message updates the specific limit it names. That is how the ITPM cap entered the model, and why Table 1 shows 8,000 TPM while the limiter plans against 7,000 input tokens.

**Daily budget.** Because the buckets refill continuously, the system can say *when* capacity returns: `deficit × 86400 / limit`. It does not say "tomorrow".

## 3. Reply mode under limits

| Situation | Mode | User sees |
|---|---|---|
| Predicted wait for the full plan ≤ 5.25 s | full | checked answer, may wait up to ~5 s |
| Full too slow, lean wait ≤ 10.5 s | lean | answer checked locally, "Quick mode" note |
| Both too slow | passages | quoted sentences with page citations, ~3 ms |
| Lean plan does not fit the daily budget | budget | quoted sentences + "about N min" until a full answer is possible |
| A model call fails mid-request with no time to retry | passages | same as above, reason logged |

## 4. Retry policy

| Rule | Value | Why |
|---|---|---|
| Retry on | 429, 5xx, timeout, connection error | these can succeed later |
| Never retry | other 4xx; a stream already shown to the user; `shed` | retrying cannot help, or would duplicate output |
| Attempts | at most 3 | p99 of a good call is < 1 s; three attempts fit in 12 s |
| Attempt timeout | 5 s read | real TTFT p99 < 1 s, so 5 s means stalled |
| Back-off | `uniform(0, min(8, 0.5 × 2^n))` | full jitter spreads retries |
| Floor | `retry-after` | the provider knows when the window clears |
| Deadline | skip a retry if `now + delay + 0.5 ≥ deadline` | do not sleep past the user's patience |
| Retry budget | +0.2 per first attempt, cap 10 | retries ≤ 20% of traffic during an outage |
| Shared cooldown | a 429 pauses all new calls until `retry-after` (≤ 30 s) | siblings would hit the same full window |
| Deadline-aware cooldown | a call whose deadline ends before the cooldown fails at once | so the request falls back instead of hanging |
| Circuit breaker | 6 consecutive non-429 failures → fail fast for 5 s | stops hammering a broken provider |

## 5. Evidence: when retrying made it worse

Same stand-in, 40 requests in 6 s, admission control off, so only the retry policy differs (`raw/storm_*.jsonl`):

| Policy | HTTP attempts | Attempts / request | 429s received | Calls served | Correct and cited % | Total p50 / p95 (ms) | Given up % |
|---|---|---|---|---|---|---|---|
| Naive: 6 tries, fixed 1–2 s, ignore retry-after, no cooldown | 239 | 5.97 | 225 | 13 | 37.5 | 9,094 / 10,450 | 0.0 |
| Bounded, first version (cooldown without deadline) | 54 | 1.35 | 2 | 13 | 5.0 | 312 / 575 | **95.0** |
| Bounded, final | 15 | 0.38 | 1 | 13 | 35.0 | 4 / 1,409 | 0.0 |

The provider serves the same 13 calls whatever the client does. The naive policy turned that into 16× the traffic and a 9 s median wait. The first bounded version showed that a cooldown without a deadline is as bad as no cooldown: users wait in it until they leave. The first-version row comes from the run before the fix, whose raw files were overwritten by the rerun; its numbers are recorded in [timeline.md](../timeline.md).
