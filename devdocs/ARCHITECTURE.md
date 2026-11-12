# Architecture: The Relay

Four views: components and their dependencies, the runtime (processes and concurrency), the request sequence, and the extension points.

## 1. Component view

```mermaid
flowchart TB
    subgraph Entry points
        ASK[ask.py<br/>CLI]
        RUN[runner.py<br/>experiments]
        PROF[profile_real.py<br/>provider probes]
        REP[report.py<br/>tables]
    end

    subgraph Core
        PIPE[pipeline.py<br/>Pipeline, tiers, checks]
        LIM[limiter.py<br/>Limiter]
        CLI[client.py<br/>LLMClient, RetryBudget]
        RET[retriever.py<br/>Retriever]
        COR[corpus.py<br/>load_docs, chunk_docs]
        TR[trace.py<br/>Trace, Span]
        CFG[config.py<br/>dataclasses]
    end

    subgraph Evaluation
        GR[grading.py]
        SI[standin.py<br/>aiohttp server]
    end

    ASK --> PIPE
    RUN --> PIPE
    RUN --> GR
    RUN -->|starts subprocess| SI
    REP --> GR
    PIPE --> LIM
    PIPE --> CLI
    PIPE --> RET
    CLI --> LIM
    RET --> COR
    PIPE --> TR
    CLI --> TR
    LIM --> TR
    SI --> RET
    GR --> COR
```

### Dependency rules

- `config.py` and `trace.py` depend on nothing in the package.
- `corpus.py` → `retriever.py` → `pipeline.py`. Retrieval never imports anything about models.
- `limiter.py` knows nothing about HTTP; `client.py` feeds it headers and 429 bodies.
- `pipeline.py` is the only module that knows about reply modes.
- `standin.py` imports only `retriever.tokenize` (to pick sentences). It does not know the papers.
- `grading.py` and `report.py` read files. They never call a model.

## 2. Runtime view

```mermaid
flowchart LR
    subgraph "Process 1: runner / ask (one asyncio event loop)"
        direction TB
        R1[request task 1] & R2[request task 2] & RN[request task N]
        R1 & R2 & RN --> ADM[Limiter.admit<br/>FIFO, deadline]
        ADM --> SEM[Limiter.slot<br/>semaphore = 8, cooldown]
        SEM --> HTTP[httpx.AsyncClient<br/>keep-alive pool 64]
    end
    HTTP -->|HTTPS, SSE| GROQ[(api.groq.com)]
    HTTP -->|HTTP, SSE| SIP
    subgraph "Process 2: stand-in (optional)"
        SIP[aiohttp on 127.0.0.1:8765<br/>windows, faults, latency model]
    end
```

- **One event loop, many tasks.** Each request is an `asyncio` task. Blocking work is limited to retrieval (~4 ms of pure Python), so no threads are needed.
- **Two gates in sequence.**
  1. `admit` reserves capacity for the whole request against the minute windows and the daily budget.
  2. `slot` caps concurrent HTTP calls at 8 and enforces the post-429 cooldown.
  Both gates honour the request's deadline and raise `Shed` instead of waiting past it.
- **The stand-in runs as a separate process.** `runner.StandIn` starts it with `python -m relay.standin`, and loads `raw/standin_calibration.json` into it on every reset. Scenario settings (limits, fault rates, remaining daily budget) are changed over its `/admin` HTTP endpoints, so the pipeline code is identical for real and stand-in runs.
- **Patience is enforced outside the pipeline.** `runner.fire` waits 15 s, then cancels the task and records `given_up`. Inside the pipeline, model calls stop at 80% of patience (12 s) so there is time left for the passages fallback.

## 3. Request sequence (full mode, happy path)

```mermaid
sequenceDiagram
    participant U as User
    participant P as Pipeline
    participant L as Limiter
    participant C as LLMClient
    participant M as Provider
    participant R as Retriever

    U->>P: handle(question)
    P->>P: plan("full") = 3 calls, tokens in/out
    P->>L: predicted_wait(plan)
    L-->>P: 0.0 s
    P->>L: admit(plan, patience)
    L-->>P: Reservation
    P->>C: complete(UNDERSTAND, json)
    C->>L: slot()
    C->>M: POST /chat/completions (stream)
    M-->>C: SSE chunks + usage, x-ratelimit-* headers
    C->>L: observe_headers()
    C-->>P: search_query, papers
    P->>R: search(question + query, k=6)
    R-->>P: hits
    P->>P: confidence(hits) ≥ 0.18
    P->>C: complete(ANSWER with [S1..S6])
    C-->>P: draft with [S#]
    P->>P: grounded(draft): ids exist, numbers in cited text
    P->>C: complete(VERIFY, json)
    C-->>P: PASS
    P->>L: settle(actual tokens, calls)
    P-->>U: stream "[Title, p.N]" answer
```

On a 429 the client calls `Limiter.on_429`, which records the limit kind and value and sets the shared cooldown. The client then sleeps for max(jittered back-off, `retry-after`), but only if that still fits before the deadline.

## 4. Deployment

There is no service deployment in this assignment. The intended shape for production, and what it needs:

| Concern | Today | For production |
|---|---|---|
| Process model | One process | N workers behind a queue |
| Limiter state | In memory; optional JSON file for daily counters | Shared store (Redis) with atomic reserve/settle |
| Index | Built at start-up in ~1 s | Same; rebuild on document change |
| Cache / single-flight | In process | Shared cache keyed by normalised question |
| Secrets | `.env` | Secret manager |
| Logs | JSON lines in `raw/` | Same schema shipped to a log store |

## 5. Extension points

| To change | Edit | Notes |
|---|---|---|
| Documents | Drop files into a folder; pass `--corpus` | `library.json` is optional |
| Provider | `RELAY_BASE_URL`, `RELAY_API_KEY`, `RELAY_MODEL` | Must speak OpenAI chat completions with SSE |
| Limits | `LimitsConfig` | Starting values only; corrected at runtime |
| Retrieval tool | Any object with `async search(query, k, rerank) -> list[Hit]` | `Tool` protocol in `pipeline.py`; `LocalSearchTool` is the default |
| Reply-mode thresholds | `PipelineConfig.full_wait_share`, `lean_wait_share`, `patience_s` | Shares of patience |
| Prompts | `UNDERSTAND_SYS`, `ANSWER_SYS`, `VERIFY_SYS`, `REVISE_SYS` in `pipeline.py` | `[[TASK]]` tags let the stand-in recognise the step |
