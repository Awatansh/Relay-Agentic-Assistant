"""Ask the library a question from the command line.

    python -m relay.ask "How many warmup steps does the Transformer use?"
    python -m relay.ask --corpus path/to/any/folder --limits-file state.json "..."
    python -m relay.ask --standin "..."          # against the local stand-in (start it first)

Works on any folder of PDF/txt/md files. Provider: Groq by default, or any
OpenAI-compatible endpoint via RELAY_BASE_URL / RELAY_API_KEY / RELAY_MODEL.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys

from .client import LLMClient
from .config import LimitsConfig, PipelineConfig, ProviderConfig, RetrievalConfig, RetryConfig
from .corpus import chunk_docs, load_docs
from .limiter import Limiter
from .pipeline import Pipeline
from .retriever import Retriever
from .trace import Trace


async def main_async(a) -> int:
    rc = RetrievalConfig(chunk_chars=a.chunk, top_k=a.k, overlap_chars=max(20, a.chunk // 6))
    pcfg = PipelineConfig(corpus_dir=a.corpus, patience_s=a.patience)
    docs = load_docs(a.corpus)
    ret = Retriever(chunk_docs(docs, rc.chunk_chars, rc.overlap_chars), aliases={d.doc_id: list(d.aliases) for d in docs})
    provider = ProviderConfig.standin() if a.standin else ProviderConfig.from_env()
    limiter = Limiter(LimitsConfig(state_file=a.limits_file))
    client = LLMClient(provider, RetryConfig(), limiter, rng=random.Random())
    pipe = Pipeline(client, limiter, ret, pcfg, rc)
    code = 0
    try:
        for q in a.question:
            tr = Trace(request_id="cli", question=q)

            async def sink(piece: str):
                sys.stdout.write(piece)
                sys.stdout.flush()

            ans = await pipe.handle(q, tr, sink)
            print()
            if ans.sources:
                print("\nSources:")
                for s in ans.sources:
                    print(f"  - {s['title']}, p.{s['page']}: {s['quote'][:140]}")
            b = tr.breakdown()
            print(f"\n[{ans.outcome}/{ans.tier}] first token {b['ttft_ms']:.0f} ms, total {b['total_ms']:.0f} ms, tokens {tr.tokens_in}+{tr.tokens_out}, waited {b['waiting_ms']:.0f} ms, 429s {tr.status_429}")
            code |= 0 if ans.outcome != "failed" else 1
    finally:
        await client.aclose()
    return code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="+")
    ap.add_argument("--corpus", default=PipelineConfig().corpus_dir)
    ap.add_argument("--standin", action="store_true")
    ap.add_argument("--chunk", type=int, default=RetrievalConfig().chunk_chars)
    ap.add_argument("--k", type=int, default=RetrievalConfig().top_k)
    ap.add_argument("--patience", type=float, default=15.0)
    ap.add_argument("--limits-file", default=None, help="persist daily counters here so a restart does not forget them")
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
