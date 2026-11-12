# Developer Documentation

Start with the HLD for the why, the Architecture for the shape, then the LLD for the how.

| Document | Read it to learn | Length |
|---|---|---|
| [HLD.md](HLD.md) | Goals, priorities when they conflict, components, main flow, risks | short |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Component and dependency view, runtime (processes, gates), request sequence, deployment, extension points | short |
| [LLD.md](LLD.md) | Every algorithm with its constants: chunking, BM25, coverage, limiter, retry loop, tier choice, checks | long |
| [DATA_MODEL.md](DATA_MODEL.md) | Every dataclass and every file format (`raw/*.jsonl`, `.meta.json`, `results.csv`, `questions.json`) | long |
| [RATE_LIMITING.md](RATE_LIMITING.md) | What Groq enforces (measured), how the limiter models it, the retry policy and the experiment behind it | medium |
| [STANDIN.md](STANDIN.md) | The stand-in server: API, latency and fault model, calibration, where it differs from Groq | medium |
| [EVALUATION.md](EVALUATION.md) | Question set, grading rules, every run, every metric definition, weaknesses | medium |
| [OPERATIONS.md](OPERATIONS.md) | Setup, commands, quota planning, troubleshooting | medium |
| [DECISIONS.md](DECISIONS.md) | 15 design decisions with alternatives, evidence and cost | medium |
| [KNOWN_ISSUES.md](KNOWN_ISSUES.md) | Limitations, defects, evaluation gaps, next steps | short |

Conventions used throughout:
- **Tier / reply mode:** `full`, `lean`, `passages`, `budget` (defined in [HLD.md](HLD.md#5-main-flow)).
- **Patience:** 15 s. The simulated user leaves after that.
- **"Correct and cited":** the grader's definition in [EVALUATION.md](EVALUATION.md#2-grading-rules).
- Every number is from a file in `raw/`, and the file is named next to it.
