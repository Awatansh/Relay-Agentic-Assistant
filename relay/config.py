"""Configuration. Everything is overridable from the environment so the same
code runs against Groq, any other OpenAI-compatible provider, or the local
stand-in server without edits."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

try:  # optional
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default, cast=str):
    v = os.environ.get(name)
    return default if v is None or v == "" else cast(v)


@dataclass(frozen=True)
class ProviderConfig:
    """One OpenAI-compatible chat-completions endpoint."""

    base_url: str = "https://api.groq.com/openai/v1"
    api_key: str = ""
    model: str = "qwen/qwen3.8-27b"
    # Extra JSON merged into every request body (e.g. {"reasoning_effort": "none"}).
    extra_body: dict = field(default_factory=lambda: {"reasoning_effort": "none"})
    name: str = "groq"
    # List price, USD per 1M tokens (Groq docs: $0.80/1.3M-per-$1 -> ~0.77 in, $4.00 out).
    price_in_per_m: float = 0.77
    price_out_per_m: float = 4.00

    @staticmethod
    def groq() -> "ProviderConfig":
        return ProviderConfig(
            api_key=os.environ.get("GROQ_API_KEY", ""),
            model=_env("GROQ_MODEL", "qwen/qwen3.8-27b"),
        )

    @staticmethod
    def from_env() -> "ProviderConfig":
        """Any OpenAI-compatible endpoint: RELAY_BASE_URL, RELAY_API_KEY, RELAY_MODEL, RELAY_EXTRA_BODY (JSON).
        With none set it targets Groq using GROQ_API_KEY."""
        base = os.environ.get("RELAY_BASE_URL")
        if not base:
            return ProviderConfig.groq()
        import json

        return ProviderConfig(
            base_url=base.rstrip("/"),
            api_key=os.environ.get("RELAY_API_KEY", ""),
            model=os.environ.get("RELAY_MODEL", "gpt-4o-mini"),
            extra_body=json.loads(os.environ.get("RELAY_EXTRA_BODY", "{}")),
            name="custom",
            price_in_per_m=float(os.environ.get("RELAY_PRICE_IN", "0")),
            price_out_per_m=float(os.environ.get("RELAY_PRICE_OUT", "0")),
        )

    @staticmethod
    def standin(port: int = 8765) -> "ProviderConfig":
        return ProviderConfig(
            base_url=f"http://127.0.0.1:{port}/v1",
            api_key="standin",
            model="standin-qwen",
            name="standin",
        )


@dataclass(frozen=True)
class LimitsConfig:
    """What the client believes the provider allows. Values are *priors*; the
    limiter corrects them from response headers and 429 bodies at runtime.

    Defaults are the ones measured/read for qwen/qwen3.8-27b on the free tier
    (see raw/real_profile.json and Table 1)."""

    rpm: int = 30
    tpm: int = 8_000
    itpm: int = 7_000  # input tokens/min: found in a 429 body, absent from headers
    rpd: int = 1_000
    tpd: int = 200_000
    max_inflight: int = 8
    safety: float = 0.90  # use at most this share of any limit
    state_file: str | None = None  # persist daily counters across restarts


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int = 3  # 1 try + 2 retries
    base_delay_s: float = 0.5
    max_delay_s: float = 8.0
    jitter: bool = True  # full jitter
    honor_retry_after: bool = True
    attempt_timeout_s: float = 5.0  # per HTTP attempt; real TTFT p99 is <1 s, so 5 s means "stuck"
    retry_budget_ratio: float = 0.2  # retries allowed per call, token-bucket style
    retry_budget_burst: int = 10
    use_budget: bool = True
    cooldown_on_429: bool = True  # tell the limiter to pause admissions


@dataclass(frozen=True)
class RetrievalConfig:
    # Chosen from Table 3 (setting A): same accuracy as the larger settings; 6 pieces leave room for
    # two papers in comparative questions. Setting F (3 pieces) matched it at 41% fewer tokens.
    chunk_chars: int = 500
    overlap_chars: int = 80
    top_k: int = 6
    rerank: bool = False
    # Used by the LEAN tier, i.e. when the token budget is tight (setting F in Table 3).
    lean_top_k: int = 3


@dataclass(frozen=True)
class PipelineConfig:
    patience_s: float = 15.0  # user gives up if no first token by then
    hard_timeout_s: float = 40.0
    understand_max_tokens: int = 160
    draft_max_tokens: int = 350
    verify_max_tokens: int = 120
    max_revisions: int = 1
    tool_timeout_s: float = 1.5
    tool_retries: int = 1
    # Admission planning: serve FULL if predicted wait is below this share of patience.
    full_wait_share: float = 0.35
    lean_wait_share: float = 0.7
    # Refuse without calling the model if no passage covers this share of the question's
    # idf-weighted vocabulary (Retriever.confidence).
    refuse_below: float = 0.18
    corpus_dir: str = str(ROOT / "corpus")


def with_(cfg, **kw):
    return replace(cfg, **kw)
