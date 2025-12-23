from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

from mygpt.config import load_config
from mygpt.rag.embeddings import embed_text, embed_texts
from mygpt.rag.vectorstore_cassandra import CassandraVectorStore


@dataclass(frozen=True)
class ChunkingConfig:
    chunk_size: int
    overlap: int


@dataclass(frozen=True)
class RAGConfig:
    top_k: int


class RAGError(RuntimeError):
    pass


def _chunking_cfg() -> ChunkingConfig:
    cfg = load_config(None)
    size = cfg.getint("rag", "chunk_size", fallback=800)
    overlap = cfg.getint("rag", "chunk_overlap", fallback=100)
    if overlap >= size:
        raise RAGError("chunk_overlap must be smaller than chunk_size")
    return ChunkingConfig(chunk_size=size, overlap=overlap)


def _rag_cfg() -> RAGConfig:
    cfg = load_config(None)
    top_k = cfg.getint("rag", "top_k", fallback=5)
    return RAGConfig(top_k=top_k)


# ----------------------------
# Chunking
# ----------------------------


def chunk_text(text: str) -> list[str]:
    """Chunk text in a word-safe, paragraph-aware way.

    Strategy:
      - Split on blank lines (paragraph boundaries).
      - Build chunks by concatenating paragraphs until `chunk_size` would be exceeded.
      - For paragraphs longer than `chunk_size`, fall back to word-safe wrapping.
      - Apply `chunk_overlap` as an overlap of trailing characters (word-safe) between chunks.

    This avoids mid-word cuts that make retrieval results hard to read.
    """

    cfg = _chunking_cfg()
    raw = (text or "").strip()
    if not raw:
        return []

    # Normalize newlines and split into paragraphs.
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    paras = [p.strip() for p in raw.split("\n\n") if p.strip()]

    def _wrap_long_paragraph(p: str) -> list[str]:
        # Word-safe wrapping for a single very long paragraph.
        words = p.split()
        out: list[str] = []
        cur: list[str] = []
        cur_len = 0
        for w in words:
            # +1 for a space if not first word
            add = len(w) + (1 if cur else 0)
            if cur and cur_len + add > cfg.chunk_size:
                out.append(" ".join(cur))
                cur = [w]
                cur_len = len(w)
            else:
                cur.append(w)
                cur_len += add
        if cur:
            out.append(" ".join(cur))
        return out

    # First pass: build chunks from paragraphs.
    chunks: list[str] = []
    cur_parts: list[str] = []
    cur_len = 0

    def _flush_current() -> None:
        nonlocal cur_parts, cur_len
        if cur_parts:
            chunks.append("\n\n".join(cur_parts).strip())
        cur_parts = []
        cur_len = 0

    for p in paras:
        parts = [p] if len(p) <= cfg.chunk_size else _wrap_long_paragraph(p)
        for part in parts:
            sep = 2 if cur_parts else 0  # \n\n
            if cur_parts and cur_len + sep + len(part) > cfg.chunk_size:
                _flush_current()
            cur_parts.append(part)
            cur_len = (cur_len + sep + len(part)) if cur_len else len(part)

    _flush_current()

    # Second pass: apply overlap as trailing characters from the previous chunk.
    if cfg.overlap <= 0 or len(chunks) <= 1:
        return chunks

    overlapped: list[str] = [chunks[0]]
    for i in range(1, len(chunks)):
        prev = overlapped[-1]
        tail = prev[-cfg.overlap :]
        # Make overlap word-safe: start at next whitespace boundary.
        if tail and not tail[0].isspace():
            j = 0
            while j < len(tail) and not tail[j].isspace():
                j += 1
            tail = tail[j:].lstrip()
        combined = (tail + "\n\n" + chunks[i]).strip() if tail else chunks[i]
        overlapped.append(combined)

    return overlapped


# ----------------------------
# Ingest
# ----------------------------


def ingest_document(
    doc_id: str,
    text: str,
    metadata: dict | None = None,
    ensure_schema: bool = False,
) -> int:
    """Chunk, embed, and store a document.

    Returns the number of chunks ingested.
    """
    chunks = chunk_text(text)
    if not chunks:
        return 0

    embeddings = embed_texts(chunks)

    metas = [metadata or {} for _ in chunks]

    store = CassandraVectorStore()
    try:
        if ensure_schema:
            # embedding dimension inferred from first vector
            store.ensure_schema(len(embeddings[0]))

        store.upsert_chunks(
            doc_id=doc_id,
            texts=chunks,
            embeddings=embeddings,
            metadatas=metas,
        )
    finally:
        store.close()

    return len(chunks)


# ----------------------------
# Retrieve
# ----------------------------


def retrieve_context(query: str, top_k: int | None = None) -> list[dict]:
    """Retrieve top-k similar chunks for a query string."""
    cfg = _rag_cfg()
    k = int(top_k) if top_k is not None else cfg.top_k
    q_emb = embed_text(query)

    store = CassandraVectorStore()
    try:
        results = store.query_by_embedding(q_emb, k=k)
    finally:
        store.close()

    return results


# ----------------------------
# Compose
# ----------------------------


def compose_context(results: Iterable[dict]) -> str:
    """Compose retrieved chunks into a single context string."""
    parts: List[str] = []
    for r in results:
        text = r.get("text", "")
        if text:
            parts.append(text.strip())
    return "\n\n".join(parts)