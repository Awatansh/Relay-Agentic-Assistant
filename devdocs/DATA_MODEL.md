# Data Model: The Relay

In-memory structures first, then every file the system reads or writes.

---

## 1. In-memory structures

### Corpus (`corpus.py`)

```python
@dataclass(frozen=True)
class Doc:
    doc_id: str                 # file name, e.g. "arxiv_1706.03762v7.pdf"
    title: str                  # from library.json, PDF metadata, or file stem
    version: str                # e.g. "arXiv:1706.03762v7" (library.json)
    pages: tuple[str, ...]      # cleaned text, index 0 = page 1
    aliases: tuple[str, ...]    # names people use, e.g. ("moshi", "helium", "mimi")

@dataclass(frozen=True)
class Chunk:
    doc_id: str
    title: str
    page: int                   # 1-based; a chunk never spans pages
    section: str                # last heading seen, e.g. "3.2.1 Scaled Dot-Product Attention"
    idx: int                    # order within the document
    text: str
    kind: str = "body"          # "body" | "references"
    # property cite -> "[title, p.page]"
```

### Retrieval (`retriever.py`)

```python
@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float                # final ranking score
    bm25: float                 # raw BM25
    coverage: float             # idf-weighted share of query terms present, 0..1
```

### Limiter (`limiter.py`)

```python
@dataclass
class Reservation:              # returned by Limiter.admit, consumed by Limiter.settle
    calls: int
    tok_in: int
    tok_out: int
    t_admit: float
    settled: bool = False

class Shed(Exception):          # cannot be served within patience
    reason: str
    retry_in_s: float
class DailyExhausted(Shed): ...
```

### Model client (`client.py`)

```python
@dataclass
class CallResult:
    text: str
    tokens_in: int; tokens_out: int        # from provider usage, else chars/4
    ttft_ms: float | None                  # first content byte of this attempt
    total_ms: float
    attempts: int; n_429: int
    streamed_to_user: bool = False
    headers: dict

class CallFailed(Exception):
    kind: str     # rate_limited | timeout | server | client | circuit | deadline | stream_broken | shed | tool
    detail: str
    retry_after: float | None
```

### Pipeline (`pipeline.py`)

```python
@dataclass
class Answer:
    text: str                   # what the user sees, with [Title, p.N] citations
    outcome: str                # answered | refused | passages | budget | failed
    tier: str                   # full | lean | passages | refuse | ...
    sources: list[dict]         # [{"title", "page", "quote"}]
    meta: dict                  # e.g. {"reason": "failed_checks"}
```

### Trace (`trace.py`)

```python
@dataclass
class Span:
    kind: str                   # wait | work | retry
    name: str                   # e.g. "admission", "draft", "draft:failed_attempt", "draft:backoff"
    t0: float; t1: float        # perf_counter seconds

@dataclass
class Trace:
    request_id: str; scenario: str; question: str
    t_send: float; t_first_token: float | None; t_end: float | None
    spans: list[Span]
    outcome: str                # answered | refused | degraded | given_up | failed
    tier: str                   # full | lean | passages | budget | cache | coalesced
    status_429: int             # 429 responses received
    llm_calls: int              # HTTP attempts made
    retries: int                # attempts beyond the first, summed over calls
    tokens_in: int; tokens_out: int
    queue_len_at_admit: int
    answer: str; error: str
    meta: dict                  # see section 2.2
```

### Configuration (`config.py`, all frozen dataclasses)

| Class | Fields (defaults) |
|---|---|
| `ProviderConfig` | `base_url` (Groq), `api_key`, `model` = `qwen/qwen3.8-27b`, `extra_body` = `{"reasoning_effort": "none"}`, `name`, `price_in_per_m` = 0.77, `price_out_per_m` = 4.00 |
| `LimitsConfig` | `rpm` 30, `tpm` 8,000, `itpm` 7,000, `rpd` 1,000, `tpd` 200,000, `max_inflight` 8, `safety` 0.9, `state_file` None |
| `RetryConfig` | `max_attempts` 3, `base_delay_s` 0.5, `max_delay_s` 8, `jitter` True, `honor_retry_after` True, `attempt_timeout_s` 5, `retry_budget_ratio` 0.2, `retry_budget_burst` 10, `use_budget` True, `cooldown_on_429` True |
| `RetrievalConfig` | `chunk_chars` 500, `overlap_chars` 80, `top_k` 6, `rerank` False, `lean_top_k` 3 |
| `PipelineConfig` | `patience_s` 15, `hard_timeout_s` 40 (not enforced, see KNOWN_ISSUES), `understand_max_tokens` 160, `draft_max_tokens` 350, `verify_max_tokens` 120, `max_revisions` 1, `tool_timeout_s` 1.5, `tool_retries` 1, `full_wait_share` 0.35, `lean_wait_share` 0.70, `refuse_below` 0.18, `corpus_dir` |

---

## 2. Files

### 2.1 Inputs

**`corpus/library.json`** (optional). Keys are file names.

```json
{ "arxiv_2410.00037v1.pdf": {
    "title": "Moshi: a speech-text foundation model for real-time dialogue",
    "version": "arXiv:2410.00037v1",
    "aliases": ["moshi", "helium", "mimi", "kyutai"] } }
```

**`eval/questions.json`**. A list of questions; `must` is a list of groups, each group a list of regex alternatives that must match the reply.

```json
{ "id": "q08", "q": "What frame rate and bitrate does Moshi's neural audio codec use?",
  "kind": "answerable", "docs": ["2410"], "must": [["12\\.5 ?Hz"], ["1\\.1 ?kbps"]] }
```

`docs` entries are substrings of `doc_id`. Unanswerable questions have `"kind": "unanswerable"` and no `docs` or `must`.

**`.env`**: `GROQ_API_KEY`, optional `GROQ_MODEL`, `RELAY_*` (see [OPERATIONS.md](OPERATIONS.md)).

### 2.2 Per-request log: `raw/<run>.jsonl`

One JSON object per request. Written by `runner.run_scenario`, `run_quality` and `smoke`.

| Field | Type | Meaning |
|---|---|---|
| `request_id` | str | `<run>-<nnn>` or `<run>-<qid>` |
| `scenario` | str | run name |
| `qid` | str | question id from `eval/questions.json` |
| `question`, `answer` | str | exactly what was asked and shown |
| `send_offset_s` | float | arrival time from the start of the run |
| `outcome` | str | `answered`, `refused`, `degraded` (passages/budget), `given_up`, `failed` |
| `tier` | str | `full`, `lean`, `passages`, `budget`, `cache`, `coalesced` |
| `ttft_ms`, `total_ms` | float or null | first piece reached the user / reply finished; null if given up |
| `first_token_at_ms` | float or null | same as `ttft_ms` |
| `waiting_ms`, `working_ms`, `retrying_ms` | float | sums of span kinds |
| `n_429` | int | 429 responses for this request |
| `retries` | int | extra attempts beyond the first, summed over calls |
| `llm_calls` | int | HTTP attempts |
| `tokens_in`, `tokens_out` | int | provider-reported usage, summed |
| `queue_len_at_admit` | int | queue length when the request joined |
| `error` | str | e.g. `gave up waiting` |
| `grade`, `grade_reason` | str | `correct_and_cited` / `wrong` / `correctly_refused` and why (recomputed by `report.py`) |
| `spans` | list | `{kind, name, start_ms, end_ms}` relative to arrival |
| `meta` | dict | see below |

Quality runs (`raw/quality_*.jsonl`) also have `setting`, `setting_desc`, `pipeline_tier`, `ttft_excl_admission_ms` and `total_excl_admission_ms`.

`meta` keys that may appear:

| Key | Meaning |
|---|---|
| `tier`, `planned_in`, `planned_out`, `predicted_wait_s` | the admission decision |
| `forced`, `admission` | evaluation switches |
| `scope` | understand step's in/out judgement |
| `retrieval_conf` | confidence of the hits used |
| `verify_log` | list of `{verdict, issues}` from the verifier |
| `revised`, `unverified_draft_dropped` | check loop results |
| `answer_outcome`, `reason`, `via` | why this reply type was chosen (`failed_checks`, `daily_budget`, `llm_rate_limited`, `low_retrieval_confidence`, ...) |
| `llm_failure` | the `CallFailed` that caused a fallback |
| `retry_in_s` | daily-budget ETA given to the user |
| `wasted_in`, `tool_failures`, `json_mode_fallback` | accounting for failed work |

### 2.3 Per-run metadata: `raw/<run>.meta.json`

| Field | Meaning |
|---|---|
| `scenario`, `backend`, `requests`, `offered_per_min`, `planned_duration_s`, `wall_s`, `note` | what was run |
| `peak_queue` | highest queue length the limiter saw |
| `client_stats` | `{attempts, retries, n429, budget_denied, circuit_open}` |
| `limiter_429_events` | up to 50 `{t, kind, retry_after, body}`; the real TPD 429 is in `burst_real.meta.json` |
| `limits_after` | the limiter's beliefs at the end (`rpm`, `tpm`, `itpm`, `rpd`) |
| `pipeline_counters` | `{cache_hit, coalesced, local_refusals, revisions, grounding_fail, llm_fallback}` |
| `standin_cfg`, `standin_counts` | stand-in settings and its own counts (`total`, `ok`, `r429`, `fail`, `hang`, `max_inflight`) |
| `admission`, `retry` | experiment switches |

### 2.4 Real-service probe log: `raw/real_profile.jsonl`

One object per HTTP exchange made by `profile_real.py`:

| Field | Meaning |
|---|---|
| `phase`, `tag` | `baseline`, `conc`, `rpm`, `rpm_recover`, `tpm`, `tpm_reserve`; e.g. `p1500_m250`, `k8` |
| `t_wall` | seconds since the probe started |
| `req_prompt_tokens`, `max_tokens` | what was requested |
| `status`, `headers` | HTTP status; `x-ratelimit-*` and `retry-after` |
| `body` | error body (non-200 only) |
| `t_headers_ms`, `ttft_ms`, `total_ms`, `chunks` | timings and streamed piece count |
| `usage` | `prompt_tokens`, `completion_tokens`, `total_tokens`, `queue_time`, `prompt_time`, `completion_time` |
| `exception` | client-side error text, if any |

`raw/real_profile_summary.json` holds percentiles, the TTFT fit and limit findings computed from it.

### 2.5 Calibration: `raw/standin_calibration.json`

```json
{ "ttft_a_ms": 314.3, "ttft_b_ms_per_tok": 0.1148, "ttft_sigma": 0.425,
  "decode_ms_per_tok": 2.183, "load_slope": 0.081,
  "_n_baseline": 17, "_waves": {"2": 242, "4": 312, "8": 426, "12": 375} }
```

Keys starting with `_` are diagnostics and are not applied to the stand-in.

### 2.6 Outputs of `report.py`

| File | Contents |
|---|---|
| `tables.md` | Tables 1, 2a, 2b, 2c, 2d, 3, the retry experiment, slow-request breakdowns |
| `tables.csv` | the same tables as CSV blocks |
| `results.csv` | one row per request from every `raw/*.jsonl` except the probe log |

`results.csv` columns: `run, backend, request_id, send_offset_s, outcome, tier, ttft_ms, total_ms, waiting_ms, working_ms, retrying_ms, n_429, retries, llm_calls, tokens_in, tokens_out, queue_len_at_admit, grade, grade_reason, error`.
