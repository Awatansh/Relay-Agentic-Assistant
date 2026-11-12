# Evaluation Method

How answers are graded, which runs were made, and exactly how every table column is computed.

## 1. Question set (`eval/questions.json`)

21 questions written for this library. Every answer was checked against the PDF text before use, and every "unanswerable" one was confirmed absent by searching all three papers.

| Kind | Count | Examples |
|---|---|---|
| Answerable, one paper | 13 | label smoothing value (0.1); Moshi theoretical/practical latency (160 ms / 200 ms); H-Net data efficiency on DNA (3.6×) |
| Answerable, two papers | 1 | GPU type for the Transformer (P100) vs Moshi (H100) |
| Not answered by the papers | 7 | carbon footprint; English-to-Japanese BLEU; Moshi on Hindi; H-Net dropout rate; H-Net GPU-hours; Moshi cloud cost; 2018 World Cup |

Several unanswerable questions are traps: the papers contain close neighbours (English-German BLEU, Transformer dropout 0.1) that a careless system would quote.

## 2. Grading rules

`relay/grading.py`, no judge model, so grades are reproducible and free.

**`correct_and_cited`** (answerable question): all of
1. The reply is not a refusal (quoted-passage replies are allowed and graded on their content).
2. Every `must` group has at least one regex matching the reply text.
3. Every expected paper is cited as `[Title, p.N]`.
4. For each expected paper, at least one cited page's PDF text contains one of the expected facts.

**`correctly_refused`** (unanswerable question): the reply refuses. Refusal means outcome `refused`, or the first 240 characters match "do not say", "not in the papers", "does not contain/cover/answer/mention", "not stated/reported/specified" and similar.

**`wrong`**: everything else. That includes an answerable question refused, a fact missing, a citation missing or pointing to a page without the fact, no reply, and an unanswerable question answered.

`report.py` recomputes every grade from the stored answer text each time it runs, so the tables always reflect the current rules.

## 3. Runs

| Run | Backend | Shape | Requests | Notes |
|---|---|---|---|---|
| `real_profile` | Groq, raw HTTP | baseline, concurrency waves, RPM burst, ITPM burst, max_tokens test | 99 | not through the pipeline |
| `steady_real` | Groq | 1 every 20 s | 12 | full tier |
| `burst_real` | Groq | 10 in 3 s, Zipf question mix, cache on | 10 | hit the real daily token limit |
| `steady` | stand-in | 1 every 20 s | 10 | faults on |
| `burst` | stand-in | 40 in 8 s, Zipf mix, cache on | 40 | an incident: many ask the same thing |
| `sustained_overload` | stand-in | 30/min for 2 min | 60 | ~10× fully checked capacity |
| `rpm_halved` | stand-in | 12/min | 20 | RPM 15, TPM 4,000, ITPM 3,500, unannounced |
| `daily_exhausted` | stand-in | 20/min | 20 | 6 requests left in the daily bucket |
| `quality_real_lean_{A,B,C,F}` | Groq | sequential, paced | 21 each | Table 3 |
| `quality_real_full_A` | Groq | sequential, paced | 21 | verifier on |
| `storm_naive_retry`, `storm_bounded_retry` | stand-in | 40 in 6 s, admission off | 40 each | retry experiment |

Stand-in fault settings for the load scenarios: 2% HTTP 503, 1% hang, 2% slow success (+6 s), 3% slow tail (×6). The simulated user waits 15 s, then the request is cancelled and logged `given_up`.

Retrieval settings:

| Id | Piece size (chars) | Overlap | Pieces | Re-rank |
|---|---|---|---|---|
| A (production) | 500 | 80 | 6 | no |
| B | 900 | 150 | 4 | yes |
| C | 1500 | 200 | 3 | yes |
| D | 900 | 150 | 4 | no (offline check only) |
| E | 900 | 150 | 2 | yes (offline check only) |
| F (lean) | 500 | 80 | 3 | no |

`python -m relay.runner offline` checks, with no model, whether the retrieved set contains all expected facts and papers for each answerable question:

| Setting | A | B | C | D | E | F |
|---|---|---|---|---|---|---|
| Evidence in retrieved set | 92.9% | 92.9% | 92.9% | 92.9% | 71.4% | 85.7% |

F retrieves less evidence offline than A, yet scored the same on the real model (57.1%). A misses q08; F misses q08 and q13. The model over-refused q13 in every setting anyway, so the loss did not show in Table 3.

## 4. Metric definitions

| Column | Definition |
|---|---|
| Duration (s) | last request's arrival + its total time, from run start |
| Offered rate (per min) | requests / planned arrival window × 60 |
| First token | time from arrival until the first piece of the reply reached the user. Replies are released only after checking, so this equals total time |
| Percentiles | `numpy.percentile` (linear interpolation) over delivered replies; n = delivered count (all requests in final runs) |
| Succeeded % | replies delivered within 15 s: full or lean answer, refusal, or quoted passages |
| Rate-limited % | requests that received at least one HTTP 429 |
| Timed out or given up % | requests cancelled at 15 s, or failed with no reply |
| Retries per request | extra HTTP attempts beyond the first, summed, divided by requests |
| Peak queue length | the most requests waiting in admission plus waiting for a connection at once |
| Tokens per answer | provider-billed prompt + completion tokens for the run / replies delivered |
| Cost per 1,000 answers | the same tokens at $0.77 per million input and $4.00 per million output (Groq list price for this model) × 1,000 / replies; the free tier charges $0 |
| Correct and cited %, Wrong %, Correctly refused % | share of the 21 questions with that grade |
| Table 3 first token / total | measured from after admission, because quality runs are paced to stay inside the limits |

## 5. Known weaknesses of the method

- 21 questions: one question moves a percentage by 4.8 points. Differences under ~10 points in Table 3 are within noise.
- Regex grading can miss correct answers phrased differently ("twelve hours" is covered, but other paraphrases may not be). All 10 `wrong` rows across the five real quality runs were read by hand. None states a false fact. They are:
  - q13, over-refused in 5 runs: the FineWeb-Edu passage was retrieved, but the model said the papers do not say. In the full-A run the JSON-mode error sent it to quoted passages instead.
  - q08, 1.1 kbps bitrate omitted, 3 runs: the answer says "passages do not specify the bitrate".
  - q11, full A: over-refused.
  - q11, lean B: names "routing module + downsampler" (the paper's p.3 description of a chunking layer) instead of "routing + smoothing". Arguably correct.
- "Succeeded" counts quoted passages as a reply. Table 2c in `tables.md` breaks the replies down by mode so the reader can apply a stricter definition.
