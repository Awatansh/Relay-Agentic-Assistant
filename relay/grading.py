"""Programmatic grading of answers. No judge model: results are reproducible and cost no tokens.

correct_and_cited  (answerable question)
    - the reply is not a refusal, AND
    - every `must` group matches the reply text (alternatives inside a group are regexes), AND
    - the reply cites every expected paper as [Title, p.N], AND
    - for each expected paper, at least one page it cites really contains an expected fact.
wrong
    - answerable but refused / missing facts / wrong or missing citation, OR
    - unanswerable but the reply does not refuse.
correctly_refused  (unanswerable question) - the reply says the papers do not say.

An answer that merely lists quoted passages (degraded mode) is graded the same way:
it must still contain the facts and cite a page that holds them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT
from .corpus import Doc

CITE_RE = re.compile(r"\[([^\[\],]{6,}?),\s*p\.(\d+)\]")
REFUSAL_RE = re.compile(r"do not say|does not say|not in (?:the )?papers|do(?:es)? not (?:contain|cover|answer|mention)|cannot be answered|not (?:stated|reported|specified)", re.I)


@dataclass
class Q:
    id: str
    q: str
    kind: str
    docs: list[str]
    must: list[list[str]]


def load_questions(path: str | Path = ROOT / "eval" / "questions.json") -> list[Q]:
    return [Q(d["id"], d["q"], d["kind"], d.get("docs", []), d.get("must", [])) for d in json.loads(Path(path).read_text(encoding="utf-8"))]


def is_refusal(text: str, outcome: str = "") -> bool:
    return outcome == "refused" or bool(REFUSAL_RE.search(text[:240]))


def grade(q: Q, text: str, outcome: str, docs: list[Doc]) -> tuple[str, str]:
    """Return (label, reason); label in correct_and_cited | wrong | correctly_refused."""
    refused = is_refusal(text, outcome)
    if q.kind == "unanswerable":
        if refused:
            return "correctly_refused", "refused"
        return "wrong", "answered an unanswerable question"
    if outcome in ("failed", "shed", "given_up", "pending") or not text:
        return "wrong", f"no answer ({outcome})"
    if refused and outcome != "passages":
        return "wrong", "refused an answerable question"
    for grp in q.must:
        if not any(re.search(p, text, re.I) for p in grp):
            return "wrong", f"missing fact {grp[0]}"
    cites = [(t.strip(), int(p)) for t, p in CITE_RE.findall(text)]
    by_key = {d.doc_id: d for d in docs}
    for key in q.docs:
        doc = next((d for d in docs if key in d.doc_id), None)
        if doc is None:
            return "wrong", f"unknown doc {key}"
        mine = [p for t, p in cites if doc.title.lower().startswith(t.lower()[:30]) or t.lower().startswith(doc.title.lower()[:30])]
        if not mine:
            return "wrong", f"no citation to {doc.title[:30]}"
        # each expected paper needs a cited page that holds at least one of the expected facts
        facts = [g[0] for g in q.must]
        if facts and not any(1 <= p <= len(doc.pages) and any(re.search(f, re.sub(r"\s+", " ", doc.pages[p - 1]), re.I) for f in facts) for p in mine):
            return "wrong", f"cited pages {mine} of {doc.title[:20]} do not contain an expected fact"
    return "correct_and_cited", "ok"
