"""Generic document ingestion: any folder of PDF / txt / md files -> page-aware chunks.

Nothing here knows about the three assignment papers. Optional metadata
(`library.json` in the folder) supplies human titles and versions; without it
the title falls back to the PDF metadata or the file name.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PAGE_MARK = re.compile(r"^---\s*PAGE\s+(\d+)\s*---\s*$", re.M)
HEADING = re.compile(r"^(?:(?:\d+|[A-Z])(?:\.\d+){0,3})\s+[A-Z][A-Za-z0-9 ,:\-–/&()'’]{2,80}$")
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")


@dataclass(frozen=True)
class Doc:
    doc_id: str
    title: str
    version: str
    pages: tuple[str, ...]  # index 0 -> page 1
    aliases: tuple[str, ...] = ()  # optional names people use for this document


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    title: str
    page: int
    section: str
    idx: int
    text: str
    kind: str = "body"  # body | references

    @property
    def cite(self) -> str:
        return f"[{self.title}, p.{self.page}]"


def _manifest(folder: Path) -> dict:
    p = folder / "library.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


NUM_ONLY = re.compile(r"^(?:\d+|[A-Z])(?:\.\d+){0,3}\.?$")


def _merge_numbered_headings(t: str) -> str:
    """PDF extraction often puts '3.2.1' and 'Scaled Dot-Product Attention' on separate lines."""
    lines = t.splitlines()
    out, i = [], 0
    while i < len(lines):
        a = lines[i].strip()
        if NUM_ONLY.match(a) and i + 1 < len(lines):
            b = lines[i + 1].strip()
            if 3 < len(b) < 80 and b[0].isupper() and not b.endswith((".", ",")) and not NUM_ONLY.match(b):
                out.append(f"{a.rstrip('.')} {b}")
                i += 2
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _clean_page(t: str) -> str:
    t = _merge_numbered_headings(t)
    t = t.replace("\u00ad", "")
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)  # de-hyphenate line breaks
    t = re.sub(r"[ \t]+", " ", t)
    return t


def load_docs(folder: str | Path) -> list[Doc]:
    folder = Path(folder)
    meta = _manifest(folder)
    docs: list[Doc] = []
    for f in sorted(folder.rglob("*")):
        if f.suffix.lower() not in (".pdf", ".txt", ".md") or f.name.startswith("."):
            continue
        m = meta.get(f.name, {})
        if f.suffix.lower() == ".pdf":
            import fitz  # PyMuPDF

            d = fitz.open(f)
            pages = [_clean_page(p.get_text()) for p in d]
            title = m.get("title") or (d.metadata or {}).get("title") or f.stem
            d.close()
        else:
            raw = f.read_text(encoding="utf-8", errors="replace")
            if PAGE_MARK.search(raw):
                parts = PAGE_MARK.split(raw)
                pages = [_clean_page(parts[i + 1]) for i in range(1, len(parts) - 1, 2)]
            else:
                pages = [_clean_page(raw)]
            title = m.get("title") or f.stem
        pages = _strip_running_headers(pages)
        docs.append(Doc(f.name, title.strip(), str(m.get("version", "")), tuple(pages), tuple(m.get("aliases", ()))))
    if not docs:
        raise FileNotFoundError(f"no pdf/txt/md documents under {folder}")
    return docs


def _strip_running_headers(pages: list[str]) -> list[str]:
    """Drop short lines repeated on many pages (running headers/footers, page numbers)."""
    if len(pages) < 4:
        return pages
    cnt: Counter = Counter()
    for p in pages:
        for ln in {l.strip() for l in p.splitlines() if 0 < len(l.strip()) < 90}:
            cnt[ln] += 1
    common = {ln for ln, c in cnt.items() if (c >= max(3, int(0.3 * len(pages))) and not ln[:1].isdigit()) or re.fullmatch(r"\d{1,3}", ln)}
    return ["\n".join(l for l in p.splitlines() if l.strip() not in common) for p in pages]


def _is_reference_like(text: str) -> bool:
    n = max(len(text), 1)
    years = len(re.findall(r"\b(?:19|20)\d{2}\b", text))
    marks = len(re.findall(r"arXiv|Proceedings|Conference|In Advances|pp\.|Journal|preprint|et al\.", text))
    return (years + marks) * 1000 / n >= 9


def chunk_docs(docs: Iterable[Doc], chunk_chars: int = 900, overlap_chars: int = 150) -> list[Chunk]:
    out: list[Chunk] = []
    for d in docs:
        section = "Front matter"
        idx = 0
        for pno, page in enumerate(d.pages, start=1):
            paras: list[str] = []
            cur: list[str] = []
            for ln in page.splitlines():
                s = ln.strip()
                if not s:
                    if cur:
                        paras.append(" ".join(cur))
                        cur = []
                    continue
                if HEADING.match(s) and len(s) < 90:
                    if cur:
                        paras.append(" ".join(cur))
                        cur = []
                    section = s
                    cur.append(s)
                    continue
                cur.append(s)
            if cur:
                paras.append(" ".join(cur))
            sents = [x for p in paras for x in SENT_SPLIT.split(p) if x.strip()]
            buf: list[str] = []
            size = 0
            sec_at_start = section
            for s in sents:
                if size + len(s) > chunk_chars and buf:
                    txt = " ".join(buf)
                    out.append(Chunk(d.doc_id, d.title, pno, sec_at_start, idx, txt, "references" if _is_reference_like(txt) else "body"))
                    idx += 1
                    # overlap: carry trailing sentences up to overlap_chars
                    keep: list[str] = []
                    k = 0
                    for t in reversed(buf):
                        if k + len(t) > overlap_chars:
                            break
                        keep.insert(0, t)
                        k += len(t)
                    buf, size = keep, k
                    sec_at_start = section
                buf.append(s)
                size += len(s)
            if buf and " ".join(buf).strip():
                txt = " ".join(buf)
                if len(txt) > 40:
                    out.append(Chunk(d.doc_id, d.title, pno, sec_at_start, idx, txt, "references" if _is_reference_like(txt) else "body"))
                    idx += 1
    return out

