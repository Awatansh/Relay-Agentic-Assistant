"""OpenAI-compatible streaming chat client with a deliberate retry policy.

The same class talks to Groq and to the local stand-in; only `ProviderConfig`
differs, so measurements transfer.

Retry policy
------------
* Retry only what can plausibly succeed: 429, 5xx, timeouts, connection errors.
  Never 4xx other than 429, never after output has already reached the user.
* Back-off = full-jitter exponential, but never shorter than `retry-after`.
* A retry is skipped if the sleep would not fit in the request's deadline.
* A *retry budget* (token bucket refilled by successful first attempts) caps the
  share of traffic that may be retries, so an outage cannot multiply load.
* A 429 puts the shared limiter in cooldown, so siblings do not retry into it.
* A small circuit breaker fails fast after repeated non-429 failures.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import httpx

from .config import ProviderConfig, RetryConfig
from .limiter import Limiter, Shed
from .trace import Trace, now


class CallFailed(Exception):
    def __init__(self, kind: str, detail: str = "", retry_after: Optional[float] = None):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind  # rate_limited|timeout|server|client|circuit|deadline|stream_broken
        self.detail = detail
        self.retry_after = retry_after


@dataclass
class CallResult:
    text: str
    tokens_in: int
    tokens_out: int
    ttft_ms: Optional[float]
    total_ms: float
    attempts: int
    n_429: int
    streamed_to_user: bool = False
    headers: dict = field(default_factory=dict)


class RetryBudget:
    """Retries may not exceed `ratio` of first attempts (plus a small burst)."""

    def __init__(self, ratio: float, burst: int, enabled: bool = True):
        self.ratio, self.cap, self.enabled = ratio, float(burst), enabled
        self.tokens = float(burst)

    def on_first_attempt(self) -> None:
        self.tokens = min(self.cap, self.tokens + self.ratio)

    def try_spend(self) -> bool:
        if not self.enabled:
            return True
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class LLMClient:
    def __init__(self, provider: ProviderConfig, retry: RetryConfig, limiter: Limiter, rng: Optional[random.Random] = None):
        self.p = provider
        self.r = retry
        self.limiter = limiter
        self.rng = rng or random.Random()
        self.budget = RetryBudget(retry.retry_budget_ratio, retry.retry_budget_burst, retry.use_budget)
        self._http = httpx.AsyncClient(
            base_url=provider.base_url,
            headers={"Authorization": f"Bearer {provider.api_key}"},
            timeout=httpx.Timeout(connect=5.0, read=retry.attempt_timeout_s, write=10.0, pool=30.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
        self._consec_fail = 0
        self._open_until = 0.0
        self.stats = {"attempts": 0, "retries": 0, "n429": 0, "budget_denied": 0, "circuit_open": 0}

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    def _backoff(self, retry_idx: int, retry_after: Optional[float]) -> float:
        cap = min(self.r.max_delay_s, self.r.base_delay_s * (2 ** retry_idx))
        d = self.rng.uniform(0, cap) if self.r.jitter else cap
        if self.r.honor_retry_after and retry_after is not None:
            d = max(d, retry_after)
        return d

    async def complete(
        self,
        system: str,
        user: str,
        *,
        name: str,
        trace: Trace,
        deadline: float,
        max_tokens: int = 300,
        temperature: float = 0.2,
        json_mode: bool = False,
        on_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> CallResult:
        body = {
            "model": self.p.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            **self.p.extra_body,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        n429 = 0
        last: Optional[CallFailed] = None
        for attempt in range(1, self.r.max_attempts + 1):
            if now() < self._open_until:
                self.stats["circuit_open"] += 1
                raise CallFailed("circuit", "provider circuit open")
            if now() >= deadline:
                raise CallFailed("deadline", "no time left")
            trace.llm_calls += 1
            self.stats["attempts"] += 1
            if attempt == 1:
                self.budget.on_first_attempt()
            t_call = None
            t_slot = now()
            try:
                try:
                    async with self.limiter.slot(trace, deadline):
                        t_call = now()
                        res = await self._once(body, deadline, on_delta, trace)
                except Shed as e:  # cooldown or connection wait would outlast the deadline
                    trace.add("wait", f"{name}:slot_shed", t_slot, now())
                    trace.llm_calls -= 1
                    self.stats["attempts"] -= 1
                    raise CallFailed("shed", str(e)) from None
                self._consec_fail = 0
                trace.add("work", name, t_call, now())
                res.attempts, res.n_429 = attempt, n429
                trace.retries += attempt - 1
                trace.tokens_in += res.tokens_in
                trace.tokens_out += res.tokens_out
                return res
            except CallFailed as e:
                last = e
                t_fail = now()
                if e.kind == "rate_limited":
                    n429 += 1
                    trace.status_429 += 1
                    self.stats["n429"] += 1
                else:
                    self._consec_fail += 1
                    if self._consec_fail >= 6:
                        self._open_until = now() + 5.0
                        self._consec_fail = 0
                # time the provider spent on a failed attempt is lost time: "retry"
                if t_call is not None:
                    trace.add("retry", f"{name}:failed_attempt", t_call, t_fail)
                    if e.kind in ("server", "timeout", "stream_broken"):
                        # ASSUMPTION (not measured): a request that reached the model but failed on our side
                        # may still count against the provider's minute windows. Charging it is the safe side.
                        trace.meta["wasted_in"] = trace.meta.get("wasted_in", 0) + (len(system) + len(user)) // 4
                retryable = e.kind in ("rate_limited", "timeout", "server")
                if e.kind == "stream_broken" or not retryable or attempt >= self.r.max_attempts:
                    trace.retries += attempt - 1
                    raise
                delay = self._backoff(attempt - 1, e.retry_after)
                if now() + delay + 0.5 >= deadline:
                    trace.retries += attempt - 1
                    raise CallFailed(e.kind, f"{e.detail}; retry would exceed deadline", e.retry_after)
                if not self.budget.try_spend():
                    self.stats["budget_denied"] += 1
                    trace.retries += attempt - 1
                    raise CallFailed(e.kind, f"{e.detail}; retry budget empty", e.retry_after)
                self.stats["retries"] += 1
                ts = now()
                await asyncio.sleep(delay)
                trace.add("retry", f"{name}:backoff", ts, now())
        raise last or CallFailed("server", "exhausted")

    # ------------------------------------------------------------------
    async def _once(self, body: dict, deadline: float, on_delta, trace: Trace) -> CallResult:
        t0 = now()
        ttft: Optional[float] = None
        parts: list[str] = []
        usage = None
        emitted = False
        timeout = max(0.5, min(self.r.attempt_timeout_s, deadline - now()))
        try:
            async with asyncio.timeout(timeout * 2.5):
                async with self._http.stream("POST", "/chat/completions", json=body, timeout=httpx.Timeout(connect=5.0, read=timeout, write=10.0, pool=30.0)) as resp:
                    self.limiter.observe_headers(resp.headers)
                    if resp.status_code != 200:
                        raw = (await resp.aread()).decode("utf-8", "replace")
                        ra = None
                        try:
                            ra = float(resp.headers.get("retry-after")) if resp.headers.get("retry-after") else None
                        except ValueError:
                            ra = None
                        if resp.status_code == 429:
                            ev = self.limiter.on_429(raw, resp.headers, ra)
                            if self.r.cooldown_on_429 is False:  # experiment switch: undo cooldown
                                self.limiter._cooldown_until = 0.0
                            raise CallFailed("rate_limited", ev["kind"] + ": " + raw[:160], ev["retry_after"])
                        kind = "server" if resp.status_code >= 500 else "client"
                        raise CallFailed(kind, f"HTTP {resp.status_code}: {raw[:160]}")
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            j = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if j.get("error"):
                            raise CallFailed("server", str(j["error"])[:160])
                        u = j.get("usage") or (j.get("x_groq") or {}).get("usage")
                        if u:
                            usage = u
                        ch = j.get("choices") or []
                        piece = ch[0].get("delta", {}).get("content") if ch else None
                        if piece:
                            if ttft is None:
                                ttft = (now() - t0) * 1000
                            parts.append(piece)
                            if on_delta is not None:
                                emitted = True
                                await on_delta(piece)
        except CallFailed as e:
            if emitted:
                raise CallFailed("stream_broken", e.detail) from e
            raise
        except (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError) as e:
            raise CallFailed("stream_broken" if emitted else "timeout", type(e).__name__) from e
        except httpx.HTTPError as e:
            raise CallFailed("stream_broken" if emitted else "server", f"{type(e).__name__}: {e}"[:160]) from e
        text = "".join(parts)
        tin = int((usage or {}).get("prompt_tokens") or 0)
        tout = int((usage or {}).get("completion_tokens") or 0)
        if not usage:  # providers that omit usage while streaming
            tin = (len(body["messages"][0]["content"]) + len(body["messages"][1]["content"])) // 4
            tout = len(text) // 4
        return CallResult(text, tin, tout, ttft, (now() - t0) * 1000, 1, 0, streamed_to_user=emitted)
