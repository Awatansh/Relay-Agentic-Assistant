# Design Decisions

Each entry: the decision, the alternatives, the evidence, and what it costs. The "Evidence" column points at files in `raw/` or rows in `tables.md`.

---

### D1. Release only checked text; first token = total time

- **Decision:** No part of a reply reaches the user until it has passed the checks for its mode.
- **Alternatives:** Stream the draft as it is generated (first token ~400 ms), then retract or amend on a failed check.
- **Why:** The brief rules out "a wrong or embarrassing answer just to stay fast". A retraction after the user has read the text does not undo it.
- **Cost:** Perceived latency. The real steady first token p50 is 1,675 ms instead of ~400 ms.

### D2. Choose the reply mode before spending quota

- **Decision:** Predict the wait for the request's whole plan. Full if ≤ 35% of patience, lean if ≤ 70%, else quoted passages.
- **Alternatives:** Always try full and degrade on failure; a plain FIFO queue.
- **Evidence:** sustained_overload (30/min, ~10× full capacity): 60/60 replies within 15 s, 0 given up. Before the deadline-aware cooldown fix, 1–2 per run gave up.
- **Cost:** Under overload most users get passages (47/60), not a written answer.

### D3. Reserve whole requests, settle afterwards

- **Decision:** `admit` charges all of a request's calls and tokens at once; `settle` corrects to actual usage.
- **Alternatives:** Rate-limit each call separately.
- **Why:** Per-call limiting under a burst lets every request finish its first call, then all of them compete for second calls. Nobody finishes.
- **Cost:** Estimates must be decent. They are learned online (chars-per-token and output-length moving averages).

### D4. Learn limits from the provider at runtime

- **Decision:** Update limits from `x-ratelimit-*` headers and from each 429's `Limit N` text.
- **Evidence:** The 7,000 input-tokens-per-minute cap is not in any header or in the rate-limits table. It surfaced only in a 429 body (`raw/real_profile.jsonl`, phase `tpm`).
- **Cost:** A parser that depends on Groq's message wording. It falls back to `retry-after` and a 2 s default.

### D5. Daily budget: no queue, honest ETA

- **Decision:** When the lean plan no longer fits the daily allowance, reply at once with quoted passages and "about N minutes".
- **Evidence:** Groq's day buckets refill continuously. The real TPD 429's "try again in 4m34.752s" equals (excess) × 86,400 / 200,000.
- **Cost:** Users at the daily limit never get a written answer until the bucket refills.

### D6. Full-jitter back-off, retry-after floor, retry budget, shared cooldown

- **Evidence:** retry experiment (`raw/storm_*.jsonl`). Naive retries: 239 attempts for 13 served calls, p50 9,094 ms. Bounded: 15 attempts for the same 13, p50 4 ms (fallback replies).
- **Cost:** Some requests that might have succeeded on a fourth try fall back to passages.

### D7. Cooldown and connection waits honour the request deadline

- **Decision:** If the post-429 cooldown would end after the request's deadline, fail the call at once so the request falls back.
- **Evidence:** Without it, the bounded-retry storm left 38/40 users waiting until they gave up (95%). With it, 0%.

### D8. Attempt timeout 5 s

- **Evidence:** Real TTFT p99 is 840 ms (17 baseline calls). Real stalls (2 of 38 calls in steady_real) produced no bytes for > 5 s, then succeeded on retry in < 0.5 s.
- **Cost:** A legitimately slow 5 s+ call would be retried. None were observed.

### D9. BM25 over 500-character chunks, 6 pieces, no re-rank

- **Alternatives:** Embeddings and a vector DB; larger chunks; the custom re-ranker.
- **Evidence:**
  - Offline evidence recall: 92.9% for A, B, C and D.
  - Real quality: all lean settings within one question (57.1–61.9%).
  - 500 × 3 (F) matched 500 × 6 (A) at 543 vs 928 tokens.
  - The first re-ranker dropped recall to 85.7%; reduced to small corrections, it only tied plain BM25.
- **Cost:** Paraphrased or non-English questions retrieve poorly. Over-refusal of q13 in every run is a retrieval-plus-model interaction this does not fix.

### D10. Refuse locally when evidence coverage < 0.18

- **Decision:** If no passage covers 18% of the question's idf-weighted vocabulary, refuse without a model call.
- **Evidence:** "carbon footprint of training" scores 0.08 (refused, 0 tokens); in-scope questions score 0.37–1.0.
- **Cost:** A badly worded in-scope question could be refused. The model's own `NOT_IN_PAPERS` contract handles the subtler cases.

### D11. Keep the model verifier in full mode, with a meaning-based prompt

- **Evidence:**
  - First version: on real steady load it rejected correct drafts (4 of 12 replies dropped to passages).
  - After giving it the drafter's passages and judging meaning: 11/12 correct and cited.
  - Table 3: still 52.4% correct with the verifier vs 57.1% without, at 1.66× the tokens.
- **Status:** Kept because it is the only check that reads the text for meaning, but the data does not show it helping. Removing it is the first thing to test next (see [KNOWN_ISSUES.md](KNOWN_ISSUES.md)).

### D12. Single-flight and a 5-minute cache in burst scenarios

- **Evidence:** stand-in burst (40 in 8 s, Zipf question mix): 20 of 40 served from cache or by joining an in-flight identical question.
- **Cost:** A stale reply if the corpus changes within 5 minutes. Off by default in the CLI.

### D13. Stand-in as a separate HTTP server

- **Alternatives:** An in-process mock client.
- **Why:** The pipeline, client, SSE parsing, headers and 429 handling are exercised byte for byte as with Groq. Only the URL differs.
- **Cost:** A subprocess to manage; scenarios configure it over `/admin`.

### D14. Programmatic grading, no judge model

- **Why:** Reproducible, free, and does not spend the quota being measured.
- **Cost:** Regex brittleness. Mitigated by reading every `wrong` row ([EVALUATION.md](EVALUATION.md#5-known-weaknesses-of-the-method)).

### D15. Replace, not patch, the first version

- **Evidence (audit of `legacy_v1/`):**
  - The Section 4 trace was a function of hard-coded default values.
  - Table 1 was static text.
  - Quality was measured against canned stand-in answers, with "any `[` in the reply" counted as cited.
  - The Moshi PDF was v2, not the required v1.
  - Real steady run: 33.3% succeeded, total p50 30,000 ms (the timeout).
- **Cost:** Time. Kept in `legacy_v1/` for reference; none of its numbers are used.
