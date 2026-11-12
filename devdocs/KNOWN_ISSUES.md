# Known Issues, Limitations and Next Steps

## 1. Limitations by design

| Limitation | Effect | What would remove it |
|---|---|---|
| Limiter state is per process | Two workers on one key each believe they own the whole quota | Shared store (Redis) with atomic reserve and settle |
| Lexical retrieval (BM25) | Paraphrased, conceptual or non-English questions retrieve poorly | Hybrid BM25 + embeddings; still cheap at this corpus size |
| No streaming of drafts | First token = total time (p50 1.7 s on real steady load) | Stream only after the local check, or stream with a visible "checking" state |
| Single-turn only | Follow-up questions lose context | Pass the previous question into the understand step |
| Quoted passages count as a served reply | Under overload, 47 of 60 people got passages, not a summary | A paid tier, or a smaller model for the lean tier |

## 2. Known defects

| # | Issue | Impact | Where |
|---|---|---|---|
| K1 | `PipelineConfig.hard_timeout_s` is computed but not enforced inside the pipeline. Patience is enforced by the caller (`runner.fire`). | A library user who does not wrap `handle` in a timeout could wait longer than 15 s if retrieval hangs repeatedly | `pipeline._run` |
| K2 | If the retrieval tool fails twice in the passages fallback, the `CallFailed("tool")` is not caught and the request ends `failed` | Rare: the local index does not fail; it would matter with a remote search tool | `pipeline._passages_answer` |
| K3 | The model sometimes echoed the `[ANSWER]` task tag at the start of its reply (1 of 21 in lean F). **Fixed after the runs:** `_finalize` strips it. The stored answer in `raw/` still shows it. | Cosmetic | `pipeline._finalize` |
| K4 | The JSON-mode fallback (retry without `response_format` on Groq's "Failed to generate JSON") was added after the final real runs. It is on the code path but has not been triggered in any logged run | Without it, q13 in `quality_real_full_A` fell to passages | `pipeline._llm_call` |
| K5 | Token accounting differs from the provider's: Groq reported 199,809 tokens used when my logs summed to ~150K | Daily planning can be optimistic | Limiter now takes the provider's remaining-requests header; there is no header for daily tokens |
| K6 | The understand step's paper names are matched by a 25-character prefix | A paraphrased paper name is ignored (soft preference only, so no harm beyond lost ranking help) | `pipeline._llm_path` |

## 3. Evaluation gaps

- 21 questions, regex-graded. Differences under ~10 points are within noise.
- One real steady run and one real burst run. The burst run hit the real daily token limit, so most of its replies were budget-mode by necessity.
- The `rpm_halved` scenario changes the stand-in's limits without telling the client. The client learns them from 429s and headers. It does not test a provider that changes limits mid-run.
- The stand-in grows latency linearly with load, while Groq appeared to batch (lower latency at 12 in flight than at 8). Overload latencies from the stand-in are therefore pessimistic.

## 4. Next steps, in priority order

1. **Decide on the verifier.** Run full-tier with and without it on a larger question set (≥ 60). If it does not raise accuracy, drop it and spend the tokens on more passages.
2. **Fix q13-type over-refusal.** The evidence is retrieved but the model says `NOT_IN_PAPERS`. Ask the model to quote the sentence it would rely on before deciding, or retry once with the top passage first.
3. **Shared limiter** for multi-worker deployment.
4. **Enforce `hard_timeout_s`** inside `Pipeline.handle` (K1) and catch tool failure in the fallback (K2).
5. **Hybrid retrieval** if the corpus grows or questions become conceptual.
