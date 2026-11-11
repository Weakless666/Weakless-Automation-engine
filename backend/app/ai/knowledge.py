"""Knowledge bases: document chunking and BM25 keyword retrieval.

Retrieval is lexical (BM25 over per-chunk term frequencies stored with each
chunk). It needs no embedding provider and is deterministic. Semantic (vector)
search is not implemented; see WAE_ARCHITECTURE.md for the extension point.
"""
from __future__ import annotations

import math
import re
import uuid
from collections import Counter

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument

_WORD = re.compile(r"[\w']+", re.UNICODE)
_STOP = frozenset("a an and are as at be but by for from has have i if in is it its of on or that the this to was were will with you your".split())
CHUNK_CHARS = 900
MAX_DOCUMENT_CHARS = 500_000


def tokenize(text: str) -> list[str]:
    return [w for w in (m.group(0).lower() for m in _WORD.finditer(text)) if len(w) > 1 and w not in _STOP]


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paragraphs:
        while len(p) > size:  # hard-split very long paragraphs on sentence/space boundaries
            cut = max(p.rfind(". ", 0, size), p.rfind(" ", 0, size))
            cut = cut + 1 if cut > size // 2 else size
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:cut].strip())
            p = p[cut:].strip()
        if len(cur) + len(p) + 2 > size and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def add_document(db: Session, kb: KnowledgeBase, title: str, content: str, source: str = "", meta: dict | None = None) -> KnowledgeDocument:
    content = content[:MAX_DOCUMENT_CHARS]
    doc = KnowledgeDocument(organization_id=kb.organization_id, knowledge_base_id=kb.id, title=title[:300], source=source[:500],
                            content=content, meta=meta or {})
    db.add(doc)
    db.flush()
    for i, chunk in enumerate(chunk_text(content)):
        terms = Counter(tokenize(f"{title} {chunk}"))
        db.add(KnowledgeChunk(organization_id=kb.organization_id, knowledge_base_id=kb.id, document_id=doc.id, position=i,
                              content=chunk, terms=dict(terms), length=sum(terms.values())))
    db.flush()
    return doc


def delete_document(db: Session, doc: KnowledgeDocument) -> None:
    db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id))
    db.delete(doc)
    db.flush()


def search(db: Session, organization_id: uuid.UUID, knowledge_base_ids: list, query: str, limit: int = 5) -> list[dict]:
    """BM25 search restricted to the organization's own knowledge bases."""
    kb_ids = []
    for k in knowledge_base_ids or []:
        try:
            kb_ids.append(k if isinstance(k, uuid.UUID) else uuid.UUID(str(k)))
        except ValueError:
            continue
    q_terms = set(tokenize(query))
    if not kb_ids or not q_terms:
        return []
    chunks = db.scalars(select(KnowledgeChunk).where(
        KnowledgeChunk.organization_id == organization_id, KnowledgeChunk.knowledge_base_id.in_(kb_ids))).all()
    if not chunks:
        return []
    n = len(chunks)
    avg_len = sum(c.length for c in chunks) / n or 1
    df = Counter()
    for c in chunks:
        for t in q_terms:
            if t in (c.terms or {}):
                df[t] += 1
    k1, b = 1.5, 0.75
    scored = []
    for c in chunks:
        score = 0.0
        for t in q_terms:
            tf = (c.terms or {}).get(t, 0)
            if not tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * c.length / avg_len))
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    docs = {d.id: d for d in db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.id.in_({c.document_id for _, c in scored[:limit]} or {uuid.uuid4()})))}
    return [{"score": round(s, 4), "content": c.content, "document_id": str(c.document_id),
             "title": docs[c.document_id].title if c.document_id in docs else "", "source": docs[c.document_id].source if c.document_id in docs else ""}
            for s, c in scored[:limit]]
