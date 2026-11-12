"""Per-request tracing. Every number in the report derives from these records.

Spans are non-overlapping stretches of a request's life, tagged:
  wait   - queued behind the limiter / concurrency cap / cooldown (nothing running for us)
  work   - something useful is running (model call that succeeded, retrieval, local compute)
  retry  - lost time due to retrying: failed attempts and back-off sleeps
The first-token timestamp is the moment the first piece of the *final reply*
reaches the user (not the first token the model emitted internally).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

now = time.perf_counter


@dataclass
class Span:
    kind: str  # wait | work | retry
    name: str
    t0: float
    t1: float

    @property
    def ms(self) -> float:
        return (self.t1 - self.t0) * 1000.0


@dataclass
class Trace:
    request_id: str
    scenario: str = ""
    question: str = ""
    t_send: float = field(default_factory=now)
    t_first_token: Optional[float] = None
    t_end: Optional[float] = None
    spans: list[Span] = field(default_factory=list)
    outcome: str = "pending"  # answered|refused|shed|budget|given_up|failed
    tier: str = ""  # full|lean|none
    status_429: int = 0  # number of 429 responses received (any attempt)
    llm_calls: int = 0  # HTTP attempts made
    retries: int = 0  # attempts beyond the first, per logical call
    tokens_in: int = 0
    tokens_out: int = 0
    queue_len_at_admit: int = 0
    answer: str = ""
    error: str = ""
    meta: dict = field(default_factory=dict)

    # -- recording helpers ------------------------------------------------
    def add(self, kind: str, name: str, t0: float, t1: float) -> None:
        if t1 > t0:
            self.spans.append(Span(kind, name, t0, t1))

    def mark_first_token(self) -> None:
        if self.t_first_token is None:
            self.t_first_token = now()

    def finish(self, outcome: str) -> None:
        if self.t_end is None:
            self.t_end = now()
        self.outcome = outcome

    # -- derived ------------------------------------------------------------
    @property
    def ttft_ms(self) -> Optional[float]:
        return None if self.t_first_token is None else (self.t_first_token - self.t_send) * 1000

    @property
    def total_ms(self) -> Optional[float]:
        return None if self.t_end is None else (self.t_end - self.t_send) * 1000

    def sum_ms(self, kind: str, before: Optional[float] = None) -> float:
        tot = 0.0
        for s in self.spans:
            if s.kind != kind:
                continue
            t1 = s.t1 if before is None else min(s.t1, before)
            if t1 > s.t0:
                tot += (t1 - s.t0) * 1000
        return tot

    def breakdown(self) -> dict:
        ft = self.t_first_token
        total = self.total_ms or 0.0
        out = {
            "total_ms": total,
            "ttft_ms": self.ttft_ms,
            "waiting_ms": self.sum_ms("wait"),
            "working_ms": self.sum_ms("work"),
            "retrying_ms": self.sum_ms("retry"),
        }
        out["unaccounted_ms"] = total - out["waiting_ms"] - out["working_ms"] - out["retrying_ms"]
        if ft is not None:
            out["before_first_token"] = {
                "waiting_ms": self.sum_ms("wait", ft),
                "working_ms": self.sum_ms("work", ft),
                "retrying_ms": self.sum_ms("retry", ft),
            }
        return out

    def to_row(self) -> dict:
        b = self.breakdown()
        return {
            "request_id": self.request_id,
            "scenario": self.scenario,
            "send_offset_s": round(self.meta.get("send_offset_s", 0.0), 3),
            "outcome": self.outcome,
            "tier": self.tier,
            "ttft_ms": None if self.ttft_ms is None else round(self.ttft_ms, 1),
            "total_ms": None if self.total_ms is None else round(self.total_ms, 1),
            "waiting_ms": round(b["waiting_ms"], 1),
            "working_ms": round(b["working_ms"], 1),
            "retrying_ms": round(b["retrying_ms"], 1),
            "n_429": self.status_429,
            "retries": self.retries,
            "llm_calls": self.llm_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "queue_len_at_admit": self.queue_len_at_admit,
            "error": self.error,
        }

    def to_json(self) -> str:
        d = self.to_row()
        d["question"] = self.question
        d["answer"] = self.answer
        d["meta"] = self.meta
        d["spans"] = [
            {"kind": s.kind, "name": s.name, "start_ms": round((s.t0 - self.t_send) * 1000, 1), "end_ms": round((s.t1 - self.t_send) * 1000, 1)}
            for s in self.spans
        ]
        d["first_token_at_ms"] = None if self.ttft_ms is None else round(self.ttft_ms, 1)
        return json.dumps(d, ensure_ascii=False)
