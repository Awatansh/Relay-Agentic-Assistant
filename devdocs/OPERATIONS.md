# Operations Guide

Setup, everyday commands, how to read the outputs, and what to do when something goes wrong.

## 1. Setup

```powershell
python --version                  # 3.12.x
pip install -r requirements.txt
copy .env.example .env            # add GROQ_API_KEY
python -m relay.runner offline    # sanity check, no API calls: prints evidence recall per setting
```

The corpus in `corpus/` is the three papers at the versions the brief requires. To re-download them:

| File | URL |
|---|---|
| `arxiv_1706.03762v7.pdf` | https://arxiv.org/pdf/1706.03762v7 |
| `arxiv_2410.00037v1.pdf` | https://arxiv.org/pdf/2410.00037v1 |
| `arxiv_2507.07955v2.pdf` | https://arxiv.org/pdf/2507.07955v2 |

Each PDF's first page carries the matching `arXiv:<id>v<n>` stamp.

## 2. Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | — | required for real runs |
| `GROQ_MODEL` | `qwen/qwen3.8-27b` | Groq model id |
| `RELAY_BASE_URL` | unset → Groq | any OpenAI-compatible base URL, e.g. `https://api.openai.com/v1` |
| `RELAY_API_KEY` | — | key for `RELAY_BASE_URL` |
| `RELAY_MODEL` | `gpt-4o-mini` | model for `RELAY_BASE_URL` |
| `RELAY_EXTRA_BODY` | `{}` | JSON merged into every request |
| `RELAY_PRICE_IN`, `RELAY_PRICE_OUT` | 0 | USD per million tokens |
| `PYTHONIOENCODING` | — | set to `utf-8` on Windows consoles to print paper symbols |

The `RELAY_*` provider is used by `relay.ask`. Experiment runs (`runner`, `profile_real`) always target Groq or the stand-in.

## 3. Commands

### Ask

```powershell
python -m relay.ask "question" ["another question" ...]
    --corpus DIR          documents to answer from (default corpus/)
    --chunk 500 --k 6     piece size and pieces fetched (defaults match setting A)
    --patience 15         seconds the user will wait
    --limits-file FILE    keep daily counters across restarts
    --standin             use a stand-in on 127.0.0.1:8765 (start it yourself first)
```

The last line printed is `[outcome/tier] first token … ms, total … ms, tokens in+out, waited … ms, 429s n`.

### Experiments

| Command | Writes |
|---|---|
| `python -m relay.profile_real [baseline conc rpm tpm]` | appends to `raw/real_profile.jsonl`, rewrites `raw/real_profile_summary.json` |
| `python -m relay.runner standin [--only steady burst ...]` | `raw/<scenario>.jsonl` + `.meta.json` for the five stand-in shapes |
| `python -m relay.runner real_load [--only steady_real burst_real]` | `raw/steady_real.*`, `raw/burst_real.*` (each waits 65 s first) |
| `python -m relay.runner quality real --tier lean --settings A B C F [--subset q01 q02]` | `raw/quality_real_lean_<S>.jsonl` |
| `python -m relay.runner quality real --tier full --settings A` | `raw/quality_real_full_A.jsonl` |
| `python -m relay.runner quality standin --settings A` | plumbing check only; stand-in answers are not real |
| `python -m relay.runner retry_storm` | `raw/storm_naive_retry.*`, `raw/storm_bounded_retry.*` |
| `python -m relay.runner smoke standin` / `smoke real` | 2 requests, prints answers and spans |
| `python -m relay.runner offline` | prints evidence recall per retrieval setting |
| `python -m relay.report` | `tables.md`, `tables.csv`, `results.csv` |

Runs overwrite their own `raw/` files. Copy `raw/` first if you want to keep an earlier run.

## 4. Quota planning (Groq free tier)

| Budget | Value | Refill |
|---|---|---|
| Tokens per day | 200,000 | 2.31 per second, continuously |
| Requests per day | 1,000 | 1 per 86.4 s |
| Input tokens per minute | 7,000 | sliding 60 s |

Approximate cost per command:

| Command | Tokens |
|---|---|
| `profile_real` | 25K |
| `real_load` | 30K |
| `quality … --tier lean` per setting | 11K–26K |
| `quality … --tier full` | 32K |
| one `relay.ask` question | ~2.9K (full) |

Running everything in one day reaches the daily limit. That is what happened during `burst_real`, and the rest of that run was served in budget mode. The provider counted about 50K more tokens than the pipeline logged, so keep a margin.

## 5. Reading the outputs

- **Quick look:** `tables.md`.
- **One request in detail:** find it in `raw/<run>.jsonl` by `request_id`. `spans` shows where the time went; `meta.reason` says why that reply type was chosen; `meta.verify_log` shows the verifier's verdicts.
- **Spreadsheet:** `results.csv`, one row per request, with grade.

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `UnicodeEncodeError: 'charmap' codec` | Windows console | `$env:PYTHONIOENCODING='utf-8'` |
| Every reply starts "Today's allowance … used up" | daily token limit reached (real 429 TPD) | wait; the reply states the ETA. Check `meta.json → limiter_429_events` |
| Replies "The answer service is busy right now" with no load | a model call failed; see `meta.llm_failure` | timeouts: provider stalls, usually transient; `client`: check the key and model id |
| `stand-in did not start` | port 8765 in use | stop the other process or change `PORT` in `runner.py` |
| `no pdf/txt/md documents under …` | wrong `--corpus` path | point it at the folder that holds the files |
| HTTP 404 `model_not_found` | model not available to your key | set `GROQ_MODEL` to one listed by `GET https://api.groq.com/openai/v1/models` |
| A run seems frozen | real runs idle 65 s on purpose, and scenarios run in real time (steady ≈ 3 min) | wait; check the timestamps in `raw/` |
