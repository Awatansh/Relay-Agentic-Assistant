"""Admission control for a rate-limited model provider.

Design points (each checked against the real service, see report §2 and §5):

* Five provider limits are modelled: RPM, TPM, ITPM (input tokens per minute; found
  by probing, it is *not* in the headers), RPD, TPD. Minute limits are sliding 60 s windows.
* A request is admitted *once, as a whole*: it reserves the calls and tokens its
  pipeline is expected to use, then settles with the real usage. Half-finished
  requests therefore never stall behind newcomers mid-pipeline.
* Waiting is FIFO with a deadline. If the predicted wait already exceeds the
  caller's patience the request is shed at once with an honest "try in N s",
  rather than parked in a queue of people who have already left.
* Limits are *learned*: response headers and 429 bodies correct the priors.
  A 429 puts the whole limiter in a short cooldown so siblings do not pile onto
  a provider that just said "stop".
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import LimitsConfig
from .trace import Trace, now


class Shed(Exception):
    """Request cannot be served within the caller's patience."""

    def __init__(self, reason: str, retry_in_s: float):
        super().__init__(f"{reason} (retry in ~{retry_in_s:.0f}s)")
        self.reason = reason
        self.retry_in_s = retry_in_s


class DailyExhausted(Shed):
    pass


@dataclass
class Reservation:
    calls: int
    tok_in: int
    tok_out: int
    t_admit: float
    settled: bool = False


_TRY_AGAIN = re.compile(r"try again in\s+(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?", re.I)
_LIMIT_N = re.compile(r"Limit\s+(\d+)", re.I)
_DUR = re.compile(r"(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?(?:([\d.]+)ms)?")


def parse_duration(s: Optional[str]) -> Optional[float]:
    """'1m26.4s' | '517ms' | '7.26s' | '2' -> seconds."""
    if not s:
        return None
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        pass
    m = _DUR.fullmatch(s)
    if not m or not any(m.groups()):
        return None
    h, mi, sec, ms = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0) + float(ms or 0) / 1000


def _wait_until_fits(win: list[tuple[float, int]], now_t: float, extra: int, need: int, cap: float) -> float:
    """Seconds until `need` fits under `cap` given window entries (t, n) and `extra` queued ahead."""
    used = sum(n for _, n in win) + extra
    if used + need <= cap:
        return 0.0
    for ts, n in win:  # oldest first
        used -= n
        if used + need <= cap:
            return max(0.0, ts + 60 - now_t)
    return 60.0


class Limiter:
    def __init__(self, limits: LimitsConfig, clock=now):
        self.cfg = limits
        self.clock = clock
        self.rpm, self.tpm, self.itpm = limits.rpm, limits.tpm, limits.itpm
        self.rpd, self.tpd = limits.rpd, limits.tpd
        self._req_win: deque[tuple[float, int]] = deque()
        self._in_win: deque[tuple[float, int]] = deque()
        self._tot_win: deque[tuple[float, int]] = deque()
        self._day_key = self._today()
        self._req_day = 0
        self._tok_day = 0
        self._cooldown_until = 0.0
        self._daily_block_until = 0.0
        self._daily_reset_hint_s: Optional[float] = None
        self._tpm_hint: Optional[tuple[int, float, float]] = None  # (remaining, t_obs, reset_s)
        self._waiters: list[object] = []
        self._pending = [0, 0]  # tokens in/out queued ahead
        self.peak_queue = 0
        self.inflight = 0
        self._sem = asyncio.Semaphore(limits.max_inflight)
        self._sem_waiting = 0
        self.events: list[dict] = []  # every 429 seen, for the raw log
        self._load_state()

    # ----- helpers --------------------------------------------------------
    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _roll(self, t: float) -> None:
        for w in (self._req_win, self._in_win, self._tot_win):
            while w and t - w[0][0] >= 60:
                w.popleft()
        if self._today() != self._day_key:
            self._day_key = self._today()
            self._req_day = self._tok_day = 0
            self._daily_block_until = 0.0

    def _cap(self, v: int) -> float:
        return v * self.cfg.safety

    @property
    def queue_len(self) -> int:
        return len(self._waiters) + self._sem_waiting

    def set_limits(self, **kw) -> None:
        """Runtime change of the believed limits (used by the 'limit halved' scenario)."""
        for k, v in kw.items():
            setattr(self, k, v)

    # ----- persistence ------------------------------------------------------
    def _load_state(self) -> None:
        p = self.cfg.state_file
        if p and Path(p).exists():
            try:
                d = json.loads(Path(p).read_text())
                if d.get("day") == self._day_key:
                    self._req_day, self._tok_day = d["req"], d["tok"]
            except Exception:
                pass

    def _save_state(self) -> None:
        p = self.cfg.state_file
        if p:
            Path(p).write_text(json.dumps({"day": self._day_key, "req": self._req_day, "tok": self._tok_day}))

    # ----- planning ---------------------------------------------------------
    def time_to_fit(self, calls: int, tok_in: int, tok_out: int, ahead_in: int = 0, ahead_out: int = 0) -> float:
        t = self.clock()
        self._roll(t)
        if tok_in > self._cap(self.itpm) or tok_in + tok_out > self._cap(self.tpm):
            return float("inf")
        w = max(
            _wait_until_fits(list(self._req_win), t, 0, calls, self._cap(self.rpm)),
            _wait_until_fits(list(self._in_win), t, ahead_in, tok_in, self._cap(self.itpm)),
            _wait_until_fits(list(self._tot_win), t, ahead_in + ahead_out, tok_in + tok_out, self._cap(self.tpm)),
            self._cooldown_until - t,
        )
        if self._tpm_hint:  # provider reports more used than we know of (shared key)
            rem, t_obs, reset_s = self._tpm_hint
            if t - t_obs < reset_s and (self.tpm - rem) + ahead_in + ahead_out + tok_in + tok_out > self._cap(self.tpm):
                w = max(w, t_obs + reset_s - t)
        return max(0.0, w)

    def predicted_wait(self, calls: int, tok_in: int, tok_out: int) -> float:
        """Wait if this request joined the queue now (everything already queued goes first)."""
        return self.time_to_fit(calls, tok_in, tok_out, self._pending[0], self._pending[1])

    def daily_status(self) -> dict:
        self._roll(self.clock())
        return {
            "req_used_day": self._req_day,
            "tok_used_day": self._tok_day,
            "req_left_frac": max(0.0, 1 - self._req_day / self.rpd),
            "tok_left_frac": max(0.0, 1 - self._tok_day / self.tpd),
        }

    def daily_ok(self, calls: int, tokens: int) -> bool:
        self._roll(self.clock())
        if self.clock() < self._daily_block_until:
            return False
        return (self._req_day + calls <= self._cap(self.rpd)) and (self._tok_day + tokens <= self._cap(self.tpd))

    def daily_reset_eta_s(self, calls: int = 1, tokens: int = 0) -> float:
        """How long until `calls`/`tokens` of daily budget are available again.

        Measured on Groq: the daily request budget refills continuously (1 request per
        86.4 s = 86400/1000), it does not reset at midnight."""
        t = self.clock()
        if self._daily_block_until > t:
            return self._daily_block_until - t
        over_req = max(0, self._req_day + calls - self._cap(self.rpd))
        over_tok = max(0, self._tok_day + tokens - self._cap(self.tpd))
        eta = max(over_req * 86400 / max(self.rpd, 1), over_tok * 86400 / max(self.tpd, 1))
        return eta if eta > 0 else (self._daily_reset_hint_s or 86400.0)

    # ----- admission ---------------------------------------------------------
    async def admit(self, calls: int, tok_in: int, tok_out: int, patience_s: float, trace: Optional[Trace] = None) -> Reservation:
        """Reserve capacity for a whole request or raise Shed/DailyExhausted."""
        t0 = self.clock()
        deadline = t0 + patience_s
        tokens = tok_in + tok_out
        if not self.daily_ok(calls, tokens):
            raise DailyExhausted("daily budget used up", self.daily_reset_eta_s(calls, tokens))
        w = self.predicted_wait(calls, tok_in, tok_out)
        if t0 + w > deadline:
            raise Shed("provider is at its per-minute limit", w if w != float("inf") else 60.0)
        ticket = object()
        self._waiters.append(ticket)
        self._pending[0] += tok_in
        self._pending[1] += tok_out
        self.peak_queue = max(self.peak_queue, self.queue_len)
        if trace is not None:
            trace.queue_len_at_admit = self.queue_len
        try:
            while True:
                t = self.clock()
                self._roll(t)
                if t >= deadline:
                    raise Shed("waited too long for capacity", self.time_to_fit(calls, tok_in, tok_out))
                if not self.daily_ok(calls, tokens):
                    raise DailyExhausted("daily budget used up", self.daily_reset_eta_s(calls, tokens))
                head = self._waiters[0] is ticket
                wait = self.time_to_fit(calls, tok_in, tok_out) if head else 0.05
                if head and wait <= 0:
                    break
                await asyncio.sleep(min(max(wait, 0.01), 0.25, max(deadline - t, 0.0)))
        finally:
            self._waiters.remove(ticket)
            self._pending[0] -= tok_in
            self._pending[1] -= tok_out
        t = self.clock()
        self._req_win.append((t, calls))
        self._in_win.append((t, tok_in))
        self._tot_win.append((t, tokens))
        self._req_day += calls
        self._tok_day += tokens
        self._save_state()
        if trace is not None:
            trace.add("wait", "admission", t0, t)
        return Reservation(calls, tok_in, tok_out, t)

    def settle(self, res: Reservation, actual_in: int, actual_out: int, calls_made: int) -> None:
        """Replace the estimate with what was really used (refund or charge)."""
        if res.settled:
            return
        res.settled = True
        t = self.clock()
        d_in, d_tot = actual_in - res.tok_in, (actual_in + actual_out) - (res.tok_in + res.tok_out)
        if d_in:
            self._in_win.append((t, d_in))
        if d_tot:
            self._tot_win.append((t, d_tot))
        d_calls = calls_made - res.calls
        if d_calls:
            self._req_win.append((t, d_calls))
        self._tok_day += d_tot
        self._req_day += d_calls
        self._save_state()

    # ----- per-call gate -------------------------------------------------------
    @asynccontextmanager
    async def slot(self, trace: Optional[Trace] = None, deadline: Optional[float] = None):
        """Concurrency cap + post-429 cooldown. If the cooldown outlasts the caller's
        deadline, raise Shed at once so the caller can fall back instead of hanging."""
        t0 = self.clock()
        if deadline is not None and self._cooldown_until > deadline:
            raise Shed("provider cooling down after a 429", self._cooldown_until - t0)
        self._sem_waiting += 1
        self.peak_queue = max(self.peak_queue, self.queue_len)
        try:
            if deadline is None:
                await self._sem.acquire()
            else:
                try:
                    await asyncio.wait_for(self._sem.acquire(), timeout=max(0.01, deadline - t0))
                except asyncio.TimeoutError:
                    raise Shed("no free connection before deadline", 1.0) from None
        finally:
            self._sem_waiting -= 1
        try:
            while True:  # honour cooldown after a 429 (counted as waiting)
                t = self.clock()
                if t >= self._cooldown_until:
                    break
                if deadline is not None and self._cooldown_until > deadline:
                    raise Shed("provider cooling down after a 429", self._cooldown_until - t)
                await asyncio.sleep(min(self._cooldown_until - t, 0.25))
            if trace is not None:
                trace.add("wait", "slot", t0, self.clock())
            self.inflight += 1
            yield
        finally:
            self.inflight -= 1
            self._sem.release()

    # ----- learning from the provider ---------------------------------------------
    def observe_headers(self, h) -> None:
        if h is None:
            return
        t = self.clock()
        try:
            lim_tok, rem_tok = h.get("x-ratelimit-limit-tokens"), h.get("x-ratelimit-remaining-tokens")
            rst_tok = parse_duration(h.get("x-ratelimit-reset-tokens"))
            if lim_tok and rem_tok is not None:
                self.tpm = int(lim_tok)
                self._tpm_hint = (int(rem_tok), t, rst_tok if rst_tok is not None else 60.0)
            lim_req, rem_req = h.get("x-ratelimit-limit-requests"), h.get("x-ratelimit-remaining-requests")
            if lim_req and rem_req is not None:
                self.rpd = int(lim_req)  # Groq: this header is per *day*
                used = int(lim_req) - int(rem_req)
                if used > self._req_day:  # other consumers of the same key
                    self._req_day = used
            rst_req = parse_duration(h.get("x-ratelimit-reset-requests"))
            if rst_req is not None:
                self._daily_reset_hint_s = rst_req
        except (ValueError, TypeError):
            pass

    def on_429(self, body_text: str, headers, retry_after: Optional[float]) -> dict:
        """Called on every 429. Learns which limit was hit and its value."""
        t = self.clock()
        low = (body_text or "").lower()
        kind = "unknown"
        if "(itpm)" in low or "input tokens per minute" in low:
            kind = "itpm"
        elif "(otpm)" in low:
            kind = "otpm"
        elif "per day" in low or "(rpd)" in low or "(tpd)" in low:
            kind = "tpd" if "token" in low else "rpd"
        elif "tokens per minute" in low or "(tpm)" in low:
            kind = "tpm"
        elif "requests per minute" in low or "(rpm)" in low:
            kind = "rpm"
        m = _LIMIT_N.search(body_text or "")
        if m and kind in ("itpm", "tpm", "rpm", "rpd", "tpd"):
            setattr(self, kind, int(m.group(1)))
        if retry_after is None:
            m2 = _TRY_AGAIN.search(body_text or "")
            if m2 and any(m2.groups()):
                hh, mm, ss = m2.groups()
                retry_after = int(hh or 0) * 3600 + int(mm or 0) * 60 + float(ss or 0)
        ra = retry_after if retry_after is not None else 2.0
        if kind in ("rpd", "tpd"):
            self._daily_block_until = max(self._daily_block_until, t + ra)
        else:
            self._cooldown_until = max(self._cooldown_until, t + min(ra, 30.0))
        ev = {"t": t, "kind": kind, "retry_after": ra, "body": (body_text or "")[:300]}
        self.events.append(ev)
        return ev

    def snapshot(self) -> dict:
        t = self.clock()
        self._roll(t)
        return {
            "itpm_used": sum(n for _, n in self._in_win),
            "itpm_cap": self.itpm,
            "tpm_used": sum(n for _, n in self._tot_win),
            "tpm_cap": self.tpm,
            "rpm_used": sum(n for _, n in self._req_win),
            "rpm_cap": self.rpm,
            "queue": self.queue_len,
            "inflight": self.inflight,
            **self.daily_status(),
        }
