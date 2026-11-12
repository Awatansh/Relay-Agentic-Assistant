# Low-Level Design: The Relay

Module by module: what each one holds, its main functions, and the exact rules it applies. Constants are the defaults in `relay/config.py` unless stated.

---

## 1. `corpus.py`: ingestion and chunking

### `load_docs(folder) -> list[Doc]`

1. Walk the folder recursively for `.pdf`, `.txt`, `.md` (hidden files skipped).
2. **PDF:** PyMuPDF, one string per page. **txt/md:** split on `--- PAGE N ---` markers if present, else one page.
3. Clean each page:
   - merge a number-only line with the title on the next line (`3.2.1` + `Scaled Dot-Product Attention`), so section headings survive extraction
   - remove soft hyphens; join words broken by a hyphen at a line end
   - collapse runs of spaces
4. Strip running headers and footers: a short line (< 90 chars) present on ≥ max(3, 30% of pages) is dropped, as is any line that is only a 1–3 digit page number.
5. Title, version, aliases come from `library.json` if present, else the PDF metadata title, else the file name.

### `chunk_docs(docs, chunk_chars=500, overlap_chars=80) -> list[Chunk]`

Per page:
1. Split into paragraphs on blank lines. A line matching the heading pattern (`3.2 Title`, `A Title`) starts a new paragraph and becomes the current section.
2. Split paragraphs into sentences at `.`, `!` or `?` followed by a capital, `(` or `[`.
3. Pack sentences into a chunk until adding the next would exceed `chunk_chars`. The next chunk starts with the trailing sentences of the previous one, up to `overlap_chars`.
4. A final remainder of ≤ 40 characters is dropped.
5. `kind = "references"` when (years + bibliography markers) × 1000 / length ≥ 9. Markers are `arXiv`, `Proceedings`, `Conference`, `In Advances`, `pp.`, `Journal`, `preprint`, `et al.`.

Chunks never cross a page boundary, so every chunk has exactly one page number to cite.

| Setting | Chunks for the three papers | Average chars |
|---|---|---|
| 500 / 80 | 1,001 | 414 |
| 900 / 150 | 524 | 745 |
| 1500 / 200 | 325 | 1,169 |

---

## 2. `retriever.py`: search

### Tokenizer

Lowercase; tokens match `[a-z0-9]+([._][a-z0-9]+)*`. Mixed tokens such as `d_k` and `h.net` are split on `.` and `_`, but pure numbers like `28.4` stay whole. 66 English and domain stop-words are removed (`paper`, `model`, `use`, ...). A crude suffix stemmer handles `ations, ation, ings, ing, ies→y, es, ed, s` when ≥ 3 characters remain.

### BM25

k1 = 1.2, b = 0.75, idf = ln(1 + (N − df + 0.5)/(df + 0.5)). The scores come from an inverted index; a query costs about 4 ms over 1,001 chunks.

### Coverage and confidence

```
weight(w)      = idf(w), or max idf if the corpus never saw w
coverage(q, c) = Σ weight(w) for w in q∩c  /  Σ weight(w) for w in q
confidence     = max coverage over the returned hits
```

Words the corpus has never seen count as informative and uncovered. "carbon footprint of training" therefore scores 0.08 and is refused, even though "training" matches everywhere.

### `search(query, k, rerank)`

- `rerank=False`: top-k by BM25. **This is the production default** (setting A).
- `rerank=True`:
  1. Take the top 5k by BM25. Score each as `0.80·bm25/max + 0.10·coverage + 0.10·bigram_overlap`, minus 0.30 if `kind == "references"`, plus 0.05 if the chunk's document is named in the query.
  2. A document is "named" when an alias word, or two title words, appears in the query.
  3. If two or more documents are named, seed the result with the best ⌈k/n⌉ chunks of each, so a comparative question sees every paper.
  4. Skip a chunk that is the immediate neighbour of one already chosen on the same page, once two hits are chosen. Overlapping neighbours add tokens, not information.

---

## 3. `limiter.py`: admission control

### State

| Field | Meaning |
|---|---|
| `_req_win`, `_in_win`, `_tot_win` | deques of `(t, n)` for the last 60 s: requests, input tokens, total tokens |
| `_req_day`, `_tok_day` | daily usage (UTC day key); optionally persisted to `state_file` |
| `rpm, tpm, itpm, rpd, tpd` | current beliefs about the limits; start from `LimitsConfig`, updated at runtime |
| `_tpm_hint` | `(remaining, t_observed, reset_s)` from the last response header |
| `_cooldown_until` | no new HTTP calls before this time (set by minute-limit 429s) |
| `_daily_block_until` | no admissions before this time (set by daily-limit 429s) |
| `_waiters`, `_pending` | FIFO tickets and the tokens they have queued |
| `_sem` | semaphore, `max_inflight = 8` |

All caps are multiplied by `safety = 0.9`.

### `time_to_fit(calls, tok_in, tok_out, ahead_in, ahead_out) -> seconds`

For each window, walk entries oldest-first, subtracting each until the new amount fits under the cap. The answer is when that entry leaves the window (`t + 60 − now`). Return the maximum over:
- the RPM, ITPM and TPM windows
- the cooldown
- the provider's own TPM hint, if the hint is still live and says more is used than we counted (another client on the same key)

It returns infinity if the request alone exceeds a cap.

### `admit(calls, tok_in, tok_out, patience_s, trace) -> Reservation`

```
if not daily_ok:                       raise DailyExhausted(eta)
if predicted_wait > patience:          raise Shed(wait)        # do not join a queue you will leave
append ticket; add tokens to _pending
loop every ≤ 0.25 s:
    if past deadline:                  raise Shed
    if daily budget gone meanwhile:    raise DailyExhausted
    if this ticket is first and time_to_fit == 0: break
remove ticket; charge windows and daily counters; record a "wait/admission" span
```

### `settle(reservation, actual_in, actual_out, calls_made)`

Appends the difference between actual and reserved usage to the windows and the daily counters (negative entries refund). `actual_in` also includes `wasted_in`, an estimate of input tokens sent on attempts that failed after reaching the provider.

### `slot(trace, deadline)` (async context manager)

1. If the cooldown already ends after the deadline, raise `Shed`.
2. Acquire the semaphore, with a timeout at the deadline.
3. Wait out the cooldown, re-checking the deadline.
4. Record a "wait/slot" span.

### Learning from the provider

- `observe_headers(h)`:
  - `x-ratelimit-limit-tokens` sets `tpm`
  - `x-ratelimit-remaining-tokens` and `x-ratelimit-reset-tokens` set `_tpm_hint`
  - `x-ratelimit-limit-requests` sets `rpd` (on Groq this header is per day)
  - limit − remaining requests raises `_req_day` if higher than our own count
- `on_429(body, headers, retry_after)` classifies the limit from the message: `(ITPM)`, `(OTPM)`, per day / `(RPD)` / `(TPD)`, `(TPM)`, `(RPM)`. It sets that limit to the `Limit N` value in the body. If there is no header, it parses `try again in 4m34.752s`.
  - Daily kinds set `_daily_block_until`.
  - Minute kinds set `_cooldown_until`, capped at 30 s.
- `daily_reset_eta_s(calls, tokens)` = max(excess requests × 86,400 / rpd, excess tokens × 86,400 / tpd). This matches the provider's continuous refill (Table 1).

---

## 4. `client.py`: model calls and retries

### `LLMClient.complete(system, user, *, name, trace, deadline, max_tokens, json_mode, on_delta) -> CallResult`

The request body is OpenAI chat completions with `stream: true`, `max_tokens`, `temperature` 0.2, `ProviderConfig.extra_body` (`reasoning_effort: "none"` for Groq), and `response_format: json_object` when asked.

```
for attempt in 1..3:
    circuit open?                        -> CallFailed("circuit")
    past deadline?                       -> CallFailed("deadline")
    async with limiter.slot(deadline):   # may raise Shed -> CallFailed("shed"), not retried
        result = _once(...)
    success: add "work" span, add tokens to trace, return
    failure:
        429                         -> count; limiter already set cooldown
        other                       -> consecutive failures += 1; at 6 open circuit for 5 s
        add "retry" span for the failed attempt
        retry only rate_limited / timeout / server, never stream_broken or client errors
        delay = uniform(0, min(8, 0.5·2^(attempt−1))), at least retry-after
        now + delay + 0.5 ≥ deadline -> give up ("retry would exceed deadline")
        retry budget empty           -> give up
        sleep delay; add "retry/backoff" span
```

### `_once` (one HTTP attempt)

- Read timeout = min(5 s, time left before deadline), with an overall guard of 2.5× that.
- Non-200: read the body. 429 goes to `limiter.on_429` → `CallFailed("rate_limited", retry_after)`; 5xx → `server`; other 4xx → `client`.
- Parse SSE `data:` lines. Usage comes from `usage` or `x_groq.usage` in the final chunk; if the provider omits it, tokens are estimated as characters / 4.
- If bytes were already forwarded to `on_delta` when the attempt fails, the failure becomes `stream_broken` and is never retried. The pipeline does not pass `on_delta` today, because it only releases checked text.

### `RetryBudget`

A token bucket: +0.2 per first attempt, capacity 10, a retry costs 1. In steady state retries cannot exceed 20% of calls.

---

## 5. `pipeline.py`: orchestration

### Plans (`plan(tier, question) -> (calls, tokens_in, tokens_out)`)

Token counts are estimated from prompt lengths divided by a chars-per-token ratio. The ratio starts at 3.8 and moves 10% towards the observed value on every call (exponential moving average). Output estimates are moving averages of the real draft and verify lengths.

| Tier | Calls | Input | Output |
|---|---|---|---|
| full | 3 | understand + draft with k = 6 passages + verify | 60 + draft + verify |
| lean | 1 | draft with k = 3 passages | draft |

### `_choose_tier`

```
lean plan does not fit today's budget                 -> "budget"   (no queue)
for (tier, share) in [(full, 0.35), (lean, 0.70)]:
    plan does not fit today's budget                  -> next
    w = limiter.predicted_wait(plan)
    w ≤ share × patience (15 s)                       -> admit(patience = share·15 − w + 0.5); on Shed -> next
                                                         else return tier
otherwise                                             -> "passages"
```

`force_tier` (evaluation only) skips this and admits the forced tier with full patience. The forced lean tier then keeps k = 6, so all Table 3 settings use their own piece count.

### `_llm_path` (full and lean)

The model-call deadline is `t_send + 0.8 × patience` = 12 s, leaving time for the passages fallback.

1. **understand** (full only, JSON mode, ≤ 160 tokens). The search query becomes `question + search_query`. Named papers are a soft preference: matching hits move to the front, nothing is filtered out.
2. **retrieve** through the `Tool`, with a 1.5 s timeout and 1 retry.
3. If confidence < 0.18 → refusal, no model call.
4. **draft** (≤ 350 tokens) from `[S1] (Title, p.N) text` passages. A draft starting with `NOT_IN_PAPERS` → refusal.
5. **check loop** (at most 1 revision):
   - `grounded(draft)`: at least one `[S#]`; every id is in range; every number of ≥ 2 characters (or containing a `.`) appears in the cited passages' text.
   - full tier and grounded → **verify** (JSON, ≤ 120 tokens) with the same passages. PASS ends the loop.
   - not OK and full tier with no revision used → **revise** with the issues; re-check.
   - otherwise → quoted passages with reason `failed_checks`.
6. **finalize:** replace each `[S#]` with `[Title, p.N]`; attach source quotes; lean tier adds the "Quick mode" note.

JSON-mode fallback: if a JSON call fails with Groq's "Failed to generate JSON", it is repeated once without JSON mode. `parse_json` accepts fenced or embedded JSON.

### Fallback replies

- `_refusal`: "The papers in this library do not say. I will not guess." plus up to two nearest `[Title, p.N]`.
- `_passages_from_hits`: up to 3 distinct pages, the 2 sentences per chunk with the most query-word overlap (≤ 300 chars), each with `[Title, p.N]`. If confidence < 0.18 it refuses instead. The header line depends on the reason:

| Reason | Header |
|---|---|
| `daily_budget` | "Today's allowance … used up" + ETA in minutes |
| `failed_checks` | "I could not confirm a written summary …" |
| anything else (capacity, model failure) | "The answer service is busy right now …" |

### Cache and single-flight (`handle`)

Enabled only when `cache_ttl_s > 0` (300 s in the burst scenarios; off in the CLI).
- The key is the normalised token list of the question.
- A second identical question waits on the first one's future (`coalesced`).
- Only `answered` and `refused` replies are cached.
- If the leader fails, followers compute their own reply.

### Delivery (`_deliver`)

The reply is emitted to the sink in 6-word pieces. The first-token time is stamped at the first piece. Outcome mapping: `answered → answered`, `refused → refused`, `passages`/`budget → degraded`, `failed → failed`.

---

## 6. `trace.py`: timing

Spans are `(kind, name, t0, t1)` on `time.perf_counter`:
- `wait`: admission, slot, coalesced
- `work`: successful model attempt, retrieval
- `retry`: failed attempt, back-off sleep, failed retrieval

`breakdown()` sums each kind, overall and before the first token. Spans do not overlap, so waiting + working + retrying = total within a few ms (the unaccounted remainder is local CPU work).

---

## 7. `grading.py`

See [EVALUATION.md](EVALUATION.md#grading-rules).

## 8. `standin.py`

See [STANDIN.md](STANDIN.md).
