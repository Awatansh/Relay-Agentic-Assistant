"""BM25 retriever with an optional cheap re-ranker. Pure Python + math.

Why BM25 and not embeddings: the library is ~3 papers (a few hundred chunks);
queries are full of exact identifiers (H-Net, d_k, 28.4 BLEU, Mimi). Lexical
match is the right tool and costs <10 ms with no model download. What it gives
up is paraphrase recall; the pipeline's query-rewrite step compensates, and
report section "How this library will mislead a naive retriever" shows what is
still missed.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from .corpus import Chunk

STOP = set(
    "a an the of in on for to and or is are was were be been being as by with from at that this these those it its "
    "what which who whom how why when where do does did can could should would will shall may might paper papers "
    "model models use used uses using based describe described say says state about between each both across".split()
)
TOK = re.compile(r"[a-z0-9]+(?:[._][a-z0-9]+)*")


def stem(w: str) -> str:
    for suf in ("ations", "ation", "ings", "ing", "ies", "es", "ed", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def tokenize(text: str) -> list[str]:
    toks = []
    for m in TOK.findall(text.lower()):
        parts = re.split(r"[._]", m) if re.search(r"[a-z]", m) else [m]
        for p in parts:
            if p and p not in STOP and (len(p) > 1 or p.isdigit()):
                toks.append(stem(p))
    return toks


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float  # final ranking score
    bm25: float
    coverage: float  # idf-weighted share of query terms present, 0..1


class Retriever:
    def __init__(self, chunks: list[Chunk], k1: float = 1.2, b: float = 0.75, aliases: dict[str, list[str]] | None = None):
        if not chunks:
            raise ValueError("empty corpus")
        self.chunks = chunks
        self.k1, self.b = k1, b
        self.tf: list[Counter] = [Counter(tokenize(c.text)) for c in chunks]
        self.dl = [sum(t.values()) for t in self.tf]
        self.avgdl = sum(self.dl) / len(self.dl)
        df: Counter = Counter()
        for t in self.tf:
            df.update(t.keys())
        n = len(chunks)
        self.idf = {w: math.log(1 + (n - d + 0.5) / (d + 0.5)) for w, d in df.items()}
        self.max_idf = max(self.idf.values())
        self.inv: dict[str, list[int]] = defaultdict(list)
        for i, t in enumerate(self.tf):
            for w in t:
                self.inv[w].append(i)
        # document-level vocabulary of title words, to detect "the question names a paper"
        self.title_terms: dict[str, set[str]] = {}
        for c in chunks:
            self.title_terms.setdefault(c.doc_id, set(tokenize(c.title)))
        # names a question may use for a document: title words plus optional aliases from the manifest
        self.alias_terms: dict[str, set[str]] = {d: set() for d in self.title_terms}
        for d, al in (aliases or {}).items():
            for a in al:
                self.alias_terms.setdefault(d, set()).update(tokenize(a))
    # ------------------------------------------------------------------
    def _bm25(self, q: list[str]) -> dict[int, float]:
        sc: dict[int, float] = defaultdict(float)
        for w in set(q):
            idf = self.idf.get(w)
            if idf is None:
                continue
            for i in self.inv[w]:
                f = self.tf[i][w]
                sc[i] += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.dl[i] / self.avgdl))
        return sc

    def _coverage(self, qset: set[str], i: int) -> float:
        # words the corpus has never seen are informative and uncovered: weight them at max idf
        wt = lambda w: self.idf.get(w, self.max_idf)
        tot = sum(wt(w) for w in qset)
        if tot == 0:
            return 0.0
        return sum(wt(w) for w in qset if w in self.tf[i]) / tot

    def named_docs(self, q: list[str]) -> list[str]:
        """Documents the question names: an alias/title word appears, or two title words do."""
        qs = set(q)
        return [d for d in self.title_terms if (self.alias_terms.get(d, set()) & qs) or len(self.title_terms[d] & qs) >= 2]

    def search(self, query: str, k: int = 4, rerank: bool = True) -> list[Hit]:
        q = tokenize(query)
        sc = self._bm25(q)
        if not sc:
            return []
        qset = set(q)
        if not rerank:
            top = sorted(sc, key=sc.get, reverse=True)[:k]
            return [Hit(self.chunks[i], sc[i], sc[i], self._coverage(qset, i)) for i in top]
        # Re-rank = BM25 stays the main signal; small corrections for what BM25 cannot see.
        named = self.named_docs(q)
        pool = sorted(sc, key=sc.get, reverse=True)[: k * 5]
        mx = sc[pool[0]] or 1.0
        qbig = {(a, b) for a, b in zip(q, q[1:])}
        scored = []
        for i in pool:
            c = self.chunks[i]
            toks = tokenize(c.text)
            phrase = len(qbig & set(zip(toks, toks[1:]))) / max(1, len(qbig))
            s = 0.80 * (sc[i] / mx) + 0.10 * self._coverage(qset, i) + 0.10 * phrase
            if c.kind == "references":
                s -= 0.30  # bibliography entries repeat paper titles, author names and years
            if c.doc_id in named:
                s += 0.05
            scored.append((s, i))
        scored.sort(key=lambda x: -x[0])

        def dedupe(cands, limit, seed=()):
            out = list(seed)
            for s, i in cands:
                c = self.chunks[i]
                if any(self.chunks[j].doc_id == c.doc_id and self.chunks[j].page == c.page and abs(self.chunks[j].idx - c.idx) == 1 for _, j in out) and len(out) >= 2:
                    continue  # overlapping neighbour: adds tokens, not information
                if (s, i) not in out:
                    out.append((s, i))
                if len(out) == limit:
                    break
            return out

        if len(named) >= 2:  # comparative question: make sure every named paper is represented
            per = max(1, -(-k // len(named)))
            seed = []
            for d in named:
                seed += [x for x in scored if self.chunks[x[1]].doc_id == d][:per]
            seed.sort(key=lambda x: -x[0])
            chosen = dedupe(scored, k, seed)
        else:
            chosen = dedupe(scored, k)
        chosen.sort(key=lambda x: -x[0])
        return [Hit(self.chunks[i], s, sc[i], self._coverage(qset, i)) for s, i in chosen[:k]]
    def confidence(self, hits: list[Hit]) -> float:
        """Best idf-weighted coverage among returned passages: 'how much of the question's
        informative vocabulary does any passage contain?'"""
        return max((h.coverage for h in hits), default=0.0)


