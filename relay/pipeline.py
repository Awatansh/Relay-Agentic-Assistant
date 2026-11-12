"""The Relay pipeline.

Steps (FULL tier):
  1 understand   LLM   rewrite the question into a search query, say whether it is in scope
  2 retrieve     TOOL  BM25 over the library (timeouts, one retry, fault-injectable)
  3 branch       local retrieval confidence too low -> refuse without spending tokens
  4 draft        LLM   answer from numbered passages, cite as [S#]
  5 check        local grounding check (ids, numbers) + LLM verify; loop: revise once
  6 deliver      map [S#] -> [Title, p.N], list sources, stream to the user

Tiers are chosen per request from the limiter's predicted wait:
  FULL      understand + draft + verify (+revise)          ~3-4 calls
  LEAN      draft only + local grounding check             1 call, no query rewrite, fewer passages
  PASSAGES  no model call: best sentences, with citations  0 calls (also the fallback on any failure)

Rule that shapes everything: a reply is only released to the user after it has
passed the checks for its tier. Nothing unchecked is streamed.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol

from .client import CallFailed, LLMClient
from .config import PipelineConfig, RetrievalConfig
from .limiter import DailyExhausted, Limiter, Shed
from .retriever import Hit, Retriever, tokenize
from .trace import Trace, now

# ----------------------------------------------------------------------- prompts
UNDERSTAND_SYS = """[[UNDERSTAND]]
You prepare a search query for a library of research papers. Library:
{library}
Return JSON only: {{"search_query": "<8-14 distinctive keywords, include exact names, symbols, numbers from the question>", "scope": "in" | "out", "papers": ["<library titles the question is about, else empty>"]}}
scope is "out" only if the question is clearly unrelated to every paper in the library."""

ANSWER_SYS = """[[ANSWER]]
Answer the question using ONLY the numbered passages below.
- Cite every claim with the passage id, like [S2]. Never invent ids.
- Copy numbers and names exactly as written in the passages.
- If the passages do not contain the answer, reply exactly: NOT_IN_PAPERS: <one sentence on what the passages do cover>
- No outside knowledge. At most 110 words.
Passages:
{passages}"""

REVISE_SYS = """[[REVISE]]
Rewrite the answer so that every claim is supported by the numbered passages and cited like [S2].
Fix these problems: {issues}
If the passages cannot support an answer reply exactly: NOT_IN_PAPERS: <one sentence>
At most 110 words.
Passages:
{passages}"""

VERIFY_SYS = """[[VERIFY]]
You check an answer against numbered passages. Paraphrase is fine; judge meaning, not wording.
Return JSON only: {{"verdict": "PASS" | "REVISE", "issues": ["<quote the exact unsupported claim>", ...]}}
Answer REVISE only if a specific claim contradicts the passages, a number differs from the passages,
or a claim appears in none of the passages. Otherwise PASS. Do not ask for extra detail or style changes.
Passages:
{passages}"""

NUM = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*(?![\w])")
CITE = re.compile(r"\[S(\d+)\]")


# ----------------------------------------------------------------------- types
@dataclass
class Answer:
    text: str
    outcome: str  # answered | refused | passages | budget | failed
    tier: str
    sources: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


class Tool(Protocol):
    async def search(self, query: str, k: int, rerank: bool) -> list[Hit]: ...


class LocalSearchTool:
    """The retrieval tool. `latency_s`, `fail_rate`, `hang_rate` simulate a remote index that misbehaves."""

    def __init__(self, retriever: Retriever, rng: Optional[random.Random] = None):
        self.r = retriever
        self.latency_s = 0.0
        self.fail_rate = 0.0
        self.hang_rate = 0.0
        self.rng = rng or random.Random(7)

    async def search(self, query: str, k: int, rerank: bool) -> list[Hit]:
        x = self.rng.random()
        if x < self.hang_rate:
            await asyncio.sleep(3600)
        if x < self.hang_rate + self.fail_rate:
            await asyncio.sleep(0.02)
            raise RuntimeError("search backend error")
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        return self.r.search(query, k, rerank)  # ~4 ms, CPU-only: not worth a thread hop


# ----------------------------------------------------------------------- helpers
def best_sentences(text: str, query: str, n: int = 2, max_chars: int = 330) -> str:
    q = set(tokenize(query))
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(\[])", text) if len(s.strip()) > 30]
    if not sents:
        return text[:max_chars]
    scored = sorted(range(len(sents)), key=lambda i: -len(q & set(tokenize(sents[i]))))[:n]
    pick = " ".join(sents[i] for i in sorted(scored))
    return pick if len(pick) <= max_chars else pick[: max_chars - 1].rsplit(" ", 1)[0] + "…"


def parse_json(text: str) -> Optional[dict]:
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def norm_q(q: str) -> str:
    return " ".join(tokenize(q))


# ----------------------------------------------------------------------- pipeline
class Pipeline:
    def __init__(
        self,
        client: LLMClient,
        limiter: Limiter,
        retriever: Retriever,
        cfg: PipelineConfig,
        rcfg: RetrievalConfig,
        tool: Optional[Tool] = None,
        *,
        use_admission: bool = True,
        use_fallback: bool = True,
        cache_ttl_s: float = 0.0,
        titles: Optional[dict[str, str]] = None,
    ):
        self.client, self.limiter, self.ret, self.cfg, self.rcfg = client, limiter, retriever, cfg, rcfg
        self.tool: Tool = tool or LocalSearchTool(retriever)
        self.use_admission = use_admission
        self.use_fallback = use_fallback
        self.force_tier: Optional[str] = None
        self.cache_ttl = cache_ttl_s
        self._cache: dict[str, tuple[float, Answer]] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self.titles = titles or {}
        self.library = "\n".join(f"- {t}" for t in sorted({c.title for c in retriever.chunks}))
        self.cpt = 3.8  # chars per token; learned from usage
        self.ema_draft_out = 150.0
        self.ema_verify_out = 40.0
        self.avg_chunk_chars = sum(len(c.text) for c in retriever.chunks) / len(retriever.chunks)
        self.counters = {"cache_hit": 0, "coalesced": 0, "local_refusals": 0, "revisions": 0, "grounding_fail": 0, "llm_fallback": 0}

    # ------------------------------------------------------------ token planning
    def _est(self, chars: int) -> int:
        return int(chars / self.cpt) + 4

    def _k(self, tier: str) -> int:
        """Passages fetched. LEAN trims to lean_top_k to save tokens, unless the tier was forced for evaluation."""
        return self.rcfg.top_k if (tier == "full" or self.force_tier) else min(self.rcfg.top_k, self.rcfg.lean_top_k)

    def plan(self, tier: str, question: str) -> tuple[int, int, int]:
        """(calls, tokens_in, tokens_out) the tier is expected to use."""
        k = self._k(tier)
        pass_chars = int(k * min(self.avg_chunk_chars, self.rcfg.chunk_chars) * 1.05) + 40 * k
        q = len(question)
        if tier == "lean":
            d_in = self._est(len(ANSWER_SYS) + pass_chars + q)
            return 1, d_in, int(self.ema_draft_out)
        u_in = self._est(len(UNDERSTAND_SYS) + len(self.library) + q)
        d_in = self._est(len(ANSWER_SYS) + pass_chars + q)
        v_in = self._est(len(VERIFY_SYS) + int(pass_chars * 0.55) + q + int(self.ema_draft_out * self.cpt))
        return 3, u_in + d_in + v_in, 60 + int(self.ema_draft_out) + int(self.ema_verify_out)

    # ------------------------------------------------------------ public
    async def handle(self, question: str, trace: Trace, sink: Optional[Callable[[str], Awaitable[None]]] = None) -> Answer:
        key = norm_q(question)
        if self.cache_ttl > 0:
            hit = self._cache.get(key)
            if hit and now() - hit[0] < self.cache_ttl:
                self.counters["cache_hit"] += 1
                trace.tier = "cache"
                return await self._deliver(hit[1], trace, sink, from_cache=True)
            fut = self._inflight.get(key)
            if fut is not None:  # single-flight: same question already being answered
                t0 = now()
                try:
                    ans = await asyncio.shield(fut)
                except Exception:  # leader failed: answer it ourselves
                    ans = None
                if ans is not None:
                    self.counters["coalesced"] += 1
                    trace.add("wait", "coalesced", t0, now())
                    trace.tier = "coalesced"
                    return await self._deliver(ans, trace, sink, from_cache=True)
            fut = asyncio.get_running_loop().create_future()
            self._inflight[key] = fut
            try:
                ans = await self._run(question, trace, sink)
            except BaseException:
                if not fut.done():
                    fut.set_exception(RuntimeError("leader failed"))
                    fut.exception()  # mark retrieved so asyncio does not warn
                raise
            finally:
                if self._inflight.get(key) is fut:
                    del self._inflight[key]
            if ans.outcome in ("answered", "refused"):
                self._cache[key] = (now(), ans)
            fut.set_result(ans)
            return ans
        return await self._run(question, trace, sink)

    # ------------------------------------------------------------ orchestration
    async def _run(self, question: str, trace: Trace, sink) -> Answer:
        cfg = self.cfg
        llm_deadline = trace.t_send + cfg.patience_s * 0.8  # after this, stop calling the model and fall back
        hard_deadline = trace.t_send + cfg.hard_timeout_s
        tier, reservation, notes = await self._choose_tier(question, trace)
        trace.tier = tier
        trace.meta["tier"] = tier
        trace.meta.update(notes)
        calls_made_before = trace.llm_calls
        tin0, tout0 = trace.tokens_in, trace.tokens_out
        ans: Optional[Answer] = None
        try:
            if tier == "budget":
                ans = await self._passages_answer(question, trace, reason="daily_budget", eta=notes.get("retry_in_s"))
            elif tier == "passages":
                ans = await self._passages_answer(question, trace, reason=notes.get("why", "capacity"))
            else:
                try:
                    ans = await self._llm_path(question, trace, tier, llm_deadline)
                except CallFailed as e:
                    trace.meta["llm_failure"] = f"{e.kind}: {e.detail}"[:200]
                    if not self.use_fallback:
                        trace.error = trace.meta["llm_failure"]
                        trace.finish("failed")
                        return Answer("Sorry, something went wrong. Please try again.", "failed", tier)
                    self.counters["llm_fallback"] += 1
                    ans = await self._passages_answer(question, trace, reason=f"llm_{e.kind}")
        finally:
            if reservation is not None:
                self.limiter.settle(reservation, trace.tokens_in - tin0 + trace.meta.get("wasted_in", 0), trace.tokens_out - tout0, trace.llm_calls - calls_made_before)
        if ans is None:
            ans = Answer("Search failed; please retry.", "failed", tier)
        return await self._deliver(ans, trace, sink)

    async def _choose_tier(self, question: str, trace: Trace):
        """Pick the richest tier whose predicted wait fits the patience budget. Never queue behind a limit we know will not clear."""
        cfg = self.cfg
        if not self.use_admission:
            return "full", None, {"admission": "off"}
        if self.force_tier:  # evaluation only: fixed tier, wait for capacity instead of degrading
            calls, tin, tout = self.plan(self.force_tier, question)
            res = await self.limiter.admit(calls, tin, tout, patience_s=cfg.patience_s, trace=trace)
            return self.force_tier, res, {"forced": True}
        lc, lin, lout = self.plan("lean", question)
        if not self.limiter.daily_ok(lc, lin + lout):
            # Even the cheapest model path does not fit in today's allowance: answer at once, no queue.
            return "budget", None, {"retry_in_s": round(self.limiter.daily_reset_eta_s(lc, lin + lout))}
        for tier, share in (("full", cfg.full_wait_share), ("lean", cfg.lean_wait_share)):
            calls, tin, tout = self.plan(tier, question)
            if not self.limiter.daily_ok(calls, tin + tout):
                continue
            w = self.limiter.predicted_wait(calls, tin, tout)
            if w <= share * cfg.patience_s:
                try:
                    res = await self.limiter.admit(calls, tin, tout, patience_s=max(0.5, share * cfg.patience_s - w + 0.5), trace=trace)
                    return tier, res, {"planned_in": tin, "planned_out": tout, "predicted_wait_s": round(w, 2)}
                except Shed:  # includes DailyExhausted
                    continue
        return "passages", None, {"why": "capacity"}

    # ------------------------------------------------------------ LLM path
    async def _llm_call(self, system: str, user: str, *, name: str, trace: Trace, deadline: float, max_tokens: int, json_mode=False):
        try:
            r = await self.client.complete(system, user, name=name, trace=trace, deadline=deadline, max_tokens=max_tokens, json_mode=json_mode)
        except CallFailed as e:
            # Seen on Groq: JSON mode sometimes rejects its own output ("Failed to generate JSON").
            # parse_json() tolerates free text, so retry once without the constraint.
            if not (json_mode and "generate json" in e.detail.lower()):
                raise
            trace.meta["json_mode_fallback"] = trace.meta.get("json_mode_fallback", 0) + 1
            r = await self.client.complete(system, user, name=name + ":nojson", trace=trace, deadline=deadline, max_tokens=max_tokens, json_mode=False)
        if r.tokens_in > 60:
            self.cpt = 0.9 * self.cpt + 0.1 * ((len(system) + len(user)) / r.tokens_in)
        return r

    async def _llm_path(self, question: str, trace: Trace, tier: str, deadline: float) -> Answer:
        cfg, rc = self.cfg, self.rcfg
        query, papers = question, []
        if tier == "full":
            r = await self._llm_call(UNDERSTAND_SYS.format(library=self.library), question, name="understand", trace=trace, deadline=deadline, max_tokens=cfg.understand_max_tokens, json_mode=True)
            j = parse_json(r.text) or {}
            q2 = j.get("search_query")
            if isinstance(q2, str) and q2.strip():
                query = f"{question} {q2}"
            papers = [p for p in (j.get("papers") or []) if isinstance(p, str)]
            trace.meta["scope"] = j.get("scope")
        k = self._k(tier)
        hits = await self._retrieve(query, k, trace)
        if papers:  # soft preference for named papers, never a hard filter (the model may mis-name one)
            named = [h for h in hits if any(p.lower()[:25] in h.chunk.title.lower() or h.chunk.title.lower()[:25] in p.lower() for p in papers)]
            hits = (named + [h for h in hits if h not in named])[:k]
        conf = self.ret.confidence(hits)
        trace.meta["retrieval_conf"] = round(conf, 3)
        if not hits or conf < cfg.refuse_below:
            self.counters["local_refusals"] += 1
            return self._refusal(question, hits, reason="low_retrieval_confidence")
        passages = self._format(hits)
        r = await self._llm_call(ANSWER_SYS.format(passages=passages), question, name="draft", trace=trace, deadline=deadline, max_tokens=cfg.draft_max_tokens)
        self.ema_draft_out = 0.8 * self.ema_draft_out + 0.2 * max(r.tokens_out, 1)
        draft = r.text.strip()
        if draft.startswith("NOT_IN_PAPERS"):
            return self._refusal(question, hits, reason="model_says_not_in_papers", note=draft)
        for rev in range(cfg.max_revisions + 1):
            ok, issues = self.grounded(draft, hits)
            if not ok:
                self.counters["grounding_fail"] += 1
            if tier == "full" and ok:
                # the verifier sees the same passages (same ids) as the drafter, so [S#] references line up
                v = await self._llm_call(VERIFY_SYS.format(passages=passages), f"Question: {question}\nAnswer: {draft}", name="verify", trace=trace, deadline=deadline, max_tokens=cfg.verify_max_tokens, json_mode=True)
                self.ema_verify_out = 0.8 * self.ema_verify_out + 0.2 * max(v.tokens_out, 1)
                vj = parse_json(v.text) or {}
                trace.meta.setdefault("verify_log", []).append({"verdict": vj.get("verdict"), "issues": vj.get("issues")})
                if str(vj.get("verdict", "PASS")).upper() == "PASS":
                    ok = True
                else:
                    ok, issues = False, [str(x) for x in (vj.get("issues") or ["unsupported claim"])][:3]
            if ok:
                return self._finalize(draft, hits, tier, trace)
            if rev >= cfg.max_revisions or tier != "full":
                break
            self.counters["revisions"] += 1
            trace.meta["revised"] = True
            rr = await self._llm_call(REVISE_SYS.format(issues="; ".join(issues), passages=passages), f"Question: {question}\nPrevious answer: {draft}", name="revise", trace=trace, deadline=deadline, max_tokens=cfg.draft_max_tokens)
            draft = rr.text.strip()
            if draft.startswith("NOT_IN_PAPERS"):
                return self._refusal(question, hits, reason="model_says_not_in_papers_after_revision", note=draft)
        # could not produce a checked answer: do not guess; show the evidence itself
        trace.meta["unverified_draft_dropped"] = True
        return await self._passages_from_hits(question, hits, reason="failed_checks")

    # ------------------------------------------------------------ retrieval tool
    async def _retrieve(self, query: str, k: int, trace: Trace) -> list[Hit]:
        last: Optional[Exception] = None
        for attempt in range(self.cfg.tool_retries + 1):
            t0 = now()
            try:
                hits = await asyncio.wait_for(self.tool.search(query, k, self.rcfg.rerank), timeout=self.cfg.tool_timeout_s)
                trace.add("work", "retrieve", t0, now())
                return hits
            except Exception as e:  # noqa: BLE001 - timeout or backend error
                last = e
                trace.add("retry", "retrieve:failed", t0, now())
                trace.meta["tool_failures"] = trace.meta.get("tool_failures", 0) + 1
        raise CallFailed("tool", f"retrieval failed: {type(last).__name__}")

    # ------------------------------------------------------------ checks and formatting
    @staticmethod
    def _format(hits: list[Hit]) -> str:
        return "\n".join(f"[S{i}] ({h.chunk.title}, p.{h.chunk.page}) {h.chunk.text}" for i, h in enumerate(hits, 1))

    @staticmethod
    def _cited(draft: str, hits: list[Hit]) -> list[Hit]:
        ids = {int(x) for x in CITE.findall(draft)}
        picked = [h for i, h in enumerate(hits, 1) if i in ids]
        return picked or hits[:2]

    @staticmethod
    def grounded(draft: str, hits: list[Hit]) -> tuple[bool, list[str]]:
        issues = []
        ids = [int(x) for x in CITE.findall(draft)]
        if not ids:
            issues.append("no [S#] citation")
        bad = [i for i in ids if not 1 <= i <= len(hits)]
        if bad:
            issues.append(f"citation ids {bad} do not exist")
        cited_text = " ".join(hits[i - 1].chunk.text for i in set(ids) if 1 <= i <= len(hits)) or " ".join(h.chunk.text for h in hits)
        body = CITE.sub("", draft)
        for n in NUM.findall(body):
            plain = n.replace(",", "")
            if (len(plain) >= 2 or "." in plain) and n not in cited_text and plain not in cited_text.replace(",", ""):
                issues.append(f"number {n} not in cited passages")
        return (not issues), issues[:4]

    def _finalize(self, draft: str, hits: list[Hit], tier: str, trace: Trace) -> Answer:
        draft = re.sub(r"^\s*\[{1,2}(?:ANSWER|REVISE)\]{1,2}\s*", "", draft)  # model sometimes echoes the task tag
        used = sorted({int(x) for x in CITE.findall(draft) if 1 <= int(x) <= len(hits)})
        def sub(m):
            h = hits[int(m.group(1)) - 1].chunk
            return f"[{h.title}, p.{h.page}]"
        text = CITE.sub(lambda m: sub(m) if 1 <= int(m.group(1)) <= len(hits) else "", draft)
        sources = [{"title": hits[i - 1].chunk.title, "page": hits[i - 1].chunk.page, "quote": best_sentences(hits[i - 1].chunk.text, draft, 1, 200)} for i in used]
        note = "" if tier == "full" else "\n(Quick mode: checked for citations and numbers, not independently reviewed by a second model pass.)"
        return Answer(text + note, "answered", tier, sources)

    def _refusal(self, question: str, hits: list[Hit], reason: str, note: str = "") -> Answer:
        close = []
        seen = set()
        for h in hits[:2]:
            k = (h.chunk.title, h.chunk.page)
            if k not in seen:
                seen.add(k)
                close.append(f"[{h.chunk.title}, p.{h.chunk.page}]")
        text = "The papers in this library do not say. I will not guess."
        if close:
            text += " The nearest material I found is " + " and ".join(close) + ", but it does not answer the question."
        return Answer(text, "refused", "refuse", [], {"reason": reason})

    async def _passages_answer(self, question: str, trace: Trace, reason: str, eta: Optional[float] = None) -> Answer:
        hits = await self._retrieve(question, self.rcfg.top_k, trace)
        return await self._passages_from_hits(question, hits, reason, eta)

    async def _passages_from_hits(self, question: str, hits: list[Hit], reason: str, eta: Optional[float] = None) -> Answer:
        conf = self.ret.confidence(hits)
        if not hits or conf < self.cfg.refuse_below:
            a = self._refusal(question, hits, reason="low_retrieval_confidence")
            a.meta["via"] = reason
            return a
        lines, sources, seen = [], [], set()
        for h in hits:
            k = (h.chunk.title, h.chunk.page)
            if k in seen:
                continue
            seen.add(k)
            q = best_sentences(h.chunk.text, question, 2, 300)
            lines.append(f"• [{h.chunk.title}, p.{h.chunk.page}] {q}")
            sources.append({"title": h.chunk.title, "page": h.chunk.page, "quote": q})
            if len(lines) == 3:
                break
        if reason == "daily_budget":
            wait = ""
            if eta:
                wait = f" The daily allowance refills gradually; roughly {eta/60:.0f} min until a full answer is possible." if eta < 7200 else " It refills gradually through the day."
            head = f"Today's allowance for written answers is used up.{wait} Here are the most relevant passages from the papers, quoted as written:"
        elif reason == "failed_checks":
            head = "I could not confirm a written summary against the papers, so here are the most relevant passages, quoted as written:"
        else:
            head = "The answer service is busy right now, so here are the most relevant passages from the papers, quoted as written (no summary):"
        return Answer(head + "\n" + "\n".join(lines), "passages", "passages", sources, {"reason": reason})

    # ------------------------------------------------------------ delivery
    async def _deliver(self, ans: Answer, trace: Trace, sink, from_cache: bool = False) -> Answer:
        trace.answer = ans.text
        words = re.findall(r"\S+\s*", ans.text)
        for i in range(0, len(words), 6):
            trace.mark_first_token()
            if sink is not None:
                await sink("".join(words[i : i + 6]))
        trace.mark_first_token()
        trace.finish({"answered": "answered", "refused": "refused", "passages": "degraded", "budget": "degraded", "failed": "failed"}[ans.outcome])
        trace.meta["answer_outcome"] = ans.outcome
        trace.meta.update({k: v for k, v in ans.meta.items() if k not in trace.meta})
        return ans
