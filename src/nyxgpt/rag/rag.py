"""Retrieval-Augmented Generation pipeline: chunking, ingestion, and retrieval.

This module ties together the RAG subsystem: splitting documents into
overlapping chunks, embedding and upserting them into the Cassandra vector
store, and retrieving relevant context for a query via hybrid (vector + BM25)
search, reciprocal-rank/weighted fusion, optional query expansion, and
optional cross-encoder reranking. It also exposes evaluation-metric helpers
and a query-result cache to avoid repeating identical retrievals.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import statistics
import time
import uuid
from collections.abc import Iterable
from configparser import ConfigParser
from dataclasses import dataclass
from typing import cast

from nyxgpt.cache import CacheBackend, DiskCache, MemoryCache, NoOpCache, hash_text
from nyxgpt.config import (
    get_rag_chat_context_max_chars,
    get_rag_chat_top_k,
    get_rag_debug_mode,
    get_rag_dedupe,
    get_rag_enabled,
    get_rag_include_headers,
    get_rag_include_scores,
    get_rag_max_chunks,
    get_rag_min_score,
    load_config,
)
from nyxgpt.rag.bm25 import BM25Index
from nyxgpt.rag.embeddings import EmbeddingDebugMetrics, embed_text, embed_texts
from nyxgpt.rag.fusion import reciprocal_rank_fusion, weighted_fusion
from nyxgpt.rag.reranker import RerankerDebugMetrics, rerank_results
from nyxgpt.rag.vectorstore_cassandra import (
    CassandraVectorStore,
    MetadataFilter,
    VectorSearchDebugMetrics,
    parse_metadata,
)
from nyxgpt.tracing import traced

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChunkingConfig:
    """Resolved settings controlling how documents are split into chunks.

    Attributes:
        chunk_size: Target maximum size (in characters) of each chunk.
        overlap: Number of characters of overlap between consecutive chunks.
        overlap_strategy: How overlap is computed: "trailing", "sentence", or
            "semantic".
        preserve_headings: Whether markdown-style headings are kept attached
            to the chunk(s) that follow them.
        sentence_aware: Whether chunk boundaries are adjusted to fall on
            sentence boundaries instead of raw character offsets.
    """

    chunk_size: int
    overlap: int
    overlap_strategy: str  # "trailing", "sentence", "semantic"
    preserve_headings: bool
    sentence_aware: bool


@dataclass
class RAGDebugInfo:
    """Complete debug information for RAG operations."""

    # Timing
    total_time_ms: float
    query_expansion_time_ms: float | None
    embedding_time_ms: float
    vector_search_time_ms: float
    keyword_search_time_ms: float | None
    fusion_time_ms: float | None
    reranking_time_ms: float | None
    filtering_time_ms: float
    composition_time_ms: float

    # Query analysis
    original_query: str
    query_variants: list[str]
    num_queries: int

    # Embedding details
    embedding_model: str
    embedding_dim: int
    num_texts_embedded: int
    batch_size: int

    # Vector search results
    raw_results_count: int
    score_min: float | None
    score_max: float | None
    score_mean: float | None

    # Hybrid search details
    hybrid_enabled: bool
    keyword_results_count: int | None
    vector_results_count: int | None
    fusion_method: str | None

    # Reranking details
    reranking_enabled: bool
    reranker_model: str | None
    num_candidates_reranked: int | None
    num_results_after_rerank: int | None

    # Filtering stats
    after_min_score_filter: int
    after_dedupe_filter: int
    after_max_chunks_filter: int

    # Context composition
    total_chars_before_truncation: int
    total_chars_after_truncation: int
    chunks_included: int

    # Collection actually queried (#3464: surfaces the effective collection
    # in the Debug tab so a scoped query's isolation is independently
    # verifiable, not just trusted from the request payload).
    collection: str = "default"


@dataclass
class RetrievalAccuracyMetrics:
    """Metrics for evaluating retrieval accuracy and quality."""

    # Retrieval success
    results_returned: int
    query_success: bool  # True if results_returned > 0

    # Coverage metrics
    unique_docs_retrieved: int
    total_chunks_retrieved: int

    # Score distribution
    score_distribution: dict[str, float]  # p50, p75, p95, p99


@dataclass
class LatencyMetrics:
    """Enhanced latency tracking with percentile breakdowns."""

    # Overall timing
    total_time_ms: float

    # Per-stage breakdowns
    stage_timings: dict[str, float]  # {stage_name: time_ms}

    # Percentile tracking (computed from historical data)
    percentiles: dict[str, float] | None = None  # p50, p95, p99


@dataclass
class HitRateMetrics:
    """Metrics for hit rate analysis and query patterns."""

    # Query success tracking
    query_success_rate: float  # % of queries returning results
    total_queries: int
    successful_queries: int
    failed_queries: int

    # Score statistics
    avg_top_score: float | None
    score_above_threshold_rate: float  # % of results above min_score


@dataclass
class RAGEvaluationMetrics:
    """Comprehensive evaluation metrics for RAG quality."""

    # Composed metrics
    retrieval_accuracy: RetrievalAccuracyMetrics
    latency: LatencyMetrics
    hit_rate: HitRateMetrics

    # Query metadata
    query_id: str  # UUID for tracking
    timestamp: float  # Unix timestamp


class RAGError(RuntimeError):
    """Raised for invalid RAG configuration or failures during ingestion/retrieval."""


# ----------------------------
# Query Result Cache
# ----------------------------
# Caches the fully retrieved/fused/reranked results of retrieve_context() so
# repeated identical queries skip vector search, BM25 indexing, fusion, and
# reranking entirely. Keyed by a fingerprint of the query plus every input
# that can change the result set. Only non-debug calls are cached, since
# debug mode exists to report *live* timing/metrics for a single call.

# Global query result cache instance (initialized lazily)
_query_result_cache: CacheBackend[list[dict]] | None = None


def _query_cache_ttl_seconds(cfg: ConfigParser, backend: str) -> int:
    """Return the effective query cache TTL for a backend.

    Mirrors the per-backend default (300s memory / 600s disk) applied when
    the cache is initialized in `_get_query_result_cache`, so callers that
    only have access to config (e.g. `get_query_cache_stats`) report the
    same TTL the cache is actually using, not `None` when it's unset.
    """
    default = 300 if backend == "memory" else 600
    return cfg.getint("cache", "query_cache_ttl_seconds", fallback=default)


def _get_query_result_cache() -> CacheBackend[list[dict]]:
    """Get or initialize the global query result cache.

    Returns:
        Initialized cache backend based on config settings
    """
    global _query_result_cache

    if _query_result_cache is not None:
        return _query_result_cache

    cfg = load_config(None)
    cache_enabled = cfg.getboolean("cache", "query_cache_enabled", fallback=False)

    if not cache_enabled:
        log.debug("Query result cache disabled")
        _query_result_cache = NoOpCache()
        return _query_result_cache

    cache_backend = cfg.get("cache", "query_cache_backend", fallback="memory").lower()

    if cache_backend == "memory":
        max_size = cfg.getint("cache", "query_cache_max_size", fallback=500)
        ttl = _query_cache_ttl_seconds(cfg, "memory")
        _query_result_cache = MemoryCache(
            max_size=max_size, default_ttl=ttl, name="rag_query_result"
        )
        log.debug(f"Query result cache initialized: memory (max_size={max_size}, ttl={ttl}s)")
    elif cache_backend == "disk":
        cache_dir = cfg.get("cache", "query_cache_dir", fallback="~/.nyxGPT/cache/queries")
        ttl = _query_cache_ttl_seconds(cfg, "disk")
        _query_result_cache = DiskCache(
            cache_dir=cache_dir, default_ttl=ttl, name="rag_query_result"
        )
        log.debug(f"Query result cache initialized: disk (dir={cache_dir}, ttl={ttl}s)")
    else:
        log.warning(f"Unknown cache backend '{cache_backend}', disabling query result cache")
        _query_result_cache = NoOpCache()

    return _query_result_cache


def clear_query_cache() -> None:
    """Clear the global query result cache.

    Called automatically whenever the underlying document set changes
    (ingestion, update, or collection deletion) so stale results are never
    served. Can also be called manually (e.g. via the /rag/cache/clear API).
    """
    global _query_result_cache
    if _query_result_cache is not None:
        _query_result_cache.clear()
        log.info("Query result cache cleared")


def get_query_cache_stats() -> dict[str, int | float | str | bool | None]:
    """Return hit rate, size, and configuration details for the query result cache.

    Returns:
        Dict with hits, misses, hit_rate, size, enabled, backend, max_size,
        ttl_seconds, and rag_enabled. Zeroed/empty (with enabled=False) if
        query result caching is disabled (`[cache] query_cache_enabled =
        false`). `rag_enabled` reflects the global RAG on/off switch
        (`get_rag_enabled`) so callers can distinguish "cache enabled but
        never exercised because RAG is off" from an actually broken cache --
        RAG can still be enabled per-chat even when the global default is
        off, so this is informational, not an error state.
    """
    cache = _get_query_result_cache()
    cfg = load_config(None)
    rag_enabled = get_rag_enabled(cfg)

    if isinstance(cache, MemoryCache):
        stats = cache.stats()
        return {
            **stats,
            "enabled": True,
            "backend": "memory",
            "ttl_seconds": _query_cache_ttl_seconds(cfg, "memory"),
            "rag_enabled": rag_enabled,
        }
    if isinstance(cache, DiskCache):
        stats = cache.stats()
        return {
            **stats,
            "enabled": True,
            "backend": "disk",
            "max_size": None,
            "ttl_seconds": _query_cache_ttl_seconds(cfg, "disk"),
            "rag_enabled": rag_enabled,
        }
    return {
        "hits": 0,
        "misses": 0,
        "hit_rate": 0.0,
        "size": 0,
        "enabled": False,
        "backend": "none",
        "max_size": None,
        "ttl_seconds": None,
        "rag_enabled": rag_enabled,
    }


def _query_cache_key(
    *,
    query: str,
    k: int,
    collection: str,
    embedding_model: str,
    embedding_dim: int | None,
    metadata_filter: MetadataFilter | None,
    min_score: float,
    max_chunks: int,
    use_expansion: bool,
    use_hybrid: bool,
    hybrid_alpha: float | None,
    reranking_enabled: bool,
) -> str:
    """Build a stable fingerprint for a retrieve_context() call.

    Includes every input that can change the returned result set, so a
    cache hit is only ever served for a byte-for-byte equivalent query.
    """
    filter_data = None
    if metadata_filter is not None:
        filter_data = {
            "doc_ids": metadata_filter.doc_ids,
            "filename": metadata_filter.filename,
            "tags": metadata_filter.tags,
            "date_from": (
                metadata_filter.date_from.isoformat() if metadata_filter.date_from else None
            ),
            "date_to": metadata_filter.date_to.isoformat() if metadata_filter.date_to else None,
        }

    key_data = {
        "query": query,
        "k": k,
        "collection": collection,
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "metadata_filter": filter_data,
        "min_score": min_score,
        "max_chunks": max_chunks,
        "use_expansion": use_expansion,
        "use_hybrid": use_hybrid,
        "hybrid_alpha": hybrid_alpha,
        "reranking_enabled": reranking_enabled,
    }
    return hash_text(json.dumps(key_data, sort_keys=True))


def _chunking_cfg() -> ChunkingConfig:
    """Load chunking settings from the `[rag]` config section.

    Returns:
        ChunkingConfig built from config.ini values (with defaults).

    Raises:
        RAGError: If `chunk_overlap` is not smaller than `chunk_size`.
    """
    cfg = load_config(None)
    size = cfg.getint("rag", "chunk_size", fallback=800)
    overlap = cfg.getint("rag", "chunk_overlap", fallback=100)
    overlap_strategy = cfg.get("rag", "overlap_strategy", fallback="trailing")
    preserve_headings = cfg.getboolean("rag", "preserve_headings", fallback=True)
    sentence_aware = cfg.getboolean("rag", "sentence_aware", fallback=True)
    if overlap >= size:
        raise RAGError("chunk_overlap must be smaller than chunk_size")
    return ChunkingConfig(
        chunk_size=size,
        overlap=overlap,
        overlap_strategy=overlap_strategy,
        preserve_headings=preserve_headings,
        sentence_aware=sentence_aware,
    )


# ----------------------------
# Chunking Utilities
# ----------------------------


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences using heuristic rules.

    Handles common sentence boundaries while preserving abbreviations
    and avoiding false splits on periods in numbers/URLs.

    Args:
        text: The text to split into sentences

    Returns:
        List of sentences (with trailing whitespace stripped)
    """
    # Common abbreviations to avoid splitting on
    abbrevs = {
        "dr",
        "mr",
        "mrs",
        "ms",
        "prof",
        "sr",
        "jr",
        "vs",
        "etc",
        "al",
        "inc",
        "ltd",
        "co",
        "mt",
        "e.g",
        "i.e",
        "u.s",
        "u.k",
        "st",
        "ave",
        "blvd",
    }

    # Sentence boundaries are found with a hand-written single-pass scanner
    # rather than a regex. A regex of the form `[.!?]+\s+(?=[A-Z])` matched
    # with `finditer` is quadratic on adversarial input: every position
    # inside a long run of punctuation (or whitespace) is retried as its own
    # match attempt when the lookahead fails, giving O(n^2) work overall
    # (CodeQL py/polynomial-redos, alert #29). This scanner advances a single
    # index monotonically, so it is O(n) regardless of input.
    sentences: list[str] = []
    last_end = 0
    n = len(text)
    i = 0

    while i < n:
        if text[i] not in ".!?":
            i += 1
            continue

        # Consume the run of sentence-ending punctuation.
        punct_end = i + 1
        while punct_end < n and text[punct_end] in ".!?":
            punct_end += 1

        # Consume the run of whitespace following it.
        ws_end = punct_end
        while ws_end < n and text[ws_end].isspace():
            ws_end += 1

        # A boundary requires at least one whitespace char and a following
        # capital letter (matching the original `(?=[A-Z])` lookahead).
        if ws_end > punct_end and ws_end < n and "A" <= text[ws_end] <= "Z":
            end_pos = ws_end
            sentence = text[last_end:end_pos].strip()

            # Check if this is an abbreviation (avoid false splits)
            if sentence:
                # Get the last word before the punctuation
                words = sentence.split()
                last_word = words[-1].rstrip(".!?").lower() if words else ""
                # If last word is an abbreviation, skip this split
                if last_word not in abbrevs:
                    sentences.append(sentence)
                    last_end = end_pos

            i = end_pos
        else:
            i = punct_end

    # Add remaining text if any
    if last_end < len(text):
        remaining = text[last_end:].strip()
        if remaining:
            sentences.append(remaining)

    return sentences if sentences else [text.strip()] if text.strip() else []


def _is_heading(line: str) -> bool:
    """Check if a line is a Markdown heading.

    Detects both ATX-style (# Heading) and Setext-style headings.

    Args:
        line: A single line of text

    Returns:
        True if the line appears to be a heading
    """
    import re

    line = line.strip()
    if not line:
        return False

    # ATX-style: # Heading, ## Heading, etc.
    # Could also check for Setext-style (underlined with === or ---)
    # but that requires looking at the next line, so we skip for simplicity
    return bool(re.match(r"^#{1,6}\s+.+", line))


def _extract_heading_level(line: str) -> int:
    """Extract heading level from a Markdown heading line.

    Args:
        line: A heading line (e.g., "## Heading")

    Returns:
        Heading level (1-6), or 0 if not a heading
    """
    import re

    match = re.match(r"^(#{1,6})\s+", line.strip())
    if match:
        return len(match.group(1))
    return 0


# ----------------------------
# Chunking
# ----------------------------


def compute_document_hash(text: str) -> str:
    """Compute SHA-256 hash of document content for change detection.

    Args:
        text: Document text content

    Returns:
        Hex-encoded SHA-256 hash
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_text(text: str) -> list[str]:
    """Chunk text with semantic boundary optimization.

    This function implements an advanced text chunking algorithm designed
    to maximize the quality of retrieval-augmented generation (RAG) results
    with support for:
    - Sentence boundary awareness
    - Heading-aware splitting (preserves Markdown heading structure)
    - Configurable overlap strategies
    - Semantic chunking (preserves paragraphs and sentences)

    **Why This Approach?**

    1. **Semantic Preservation**: Splitting on sentence and paragraph boundaries
       keeps related ideas together, improving retrieval relevance.

    2. **Heading Awareness**: Markdown headings are preserved with their content,
       ensuring context hierarchy is maintained.

    3. **Readability**: Chunks respect natural text boundaries (sentences, paragraphs)
       so retrieved content is human-readable and coherent.

    4. **Context Continuity**: Configurable overlap strategies ensure that even if
       the most relevant sentence is at a chunk boundary, surrounding context is
       preserved.

    **Algorithm Overview:**

    Phase 1 - Heading-aware section extraction (if enabled):
      - Detect Markdown headings (# Heading format)
      - Group content under each heading as a logical section
      - Preserve heading-content relationships

    Phase 2 - Semantic chunking:
      - Split sections into paragraphs (blank line boundaries)
      - If sentence-aware mode enabled, split long paragraphs into sentences
      - Build chunks by concatenating semantic units until chunk_size reached
      - Prefer breaking at sentence boundaries over word boundaries

    Phase 3 - Overlap application (configurable strategies):
      - "trailing": Overlap with trailing characters from previous chunk (legacy)
      - "sentence": Overlap with complete sentences from previous chunk
      - "semantic": Overlap with complete paragraphs/sections when possible

    **Configuration:**

    Controlled by `[rag]` section in config.ini:
    - `chunk_size`: Target maximum characters per chunk (default: 800)
    - `chunk_overlap`: Characters to overlap between chunks (default: 100)
    - `overlap_strategy`: "trailing", "sentence", or "semantic" (default: "trailing")
    - `preserve_headings`: Keep headings with content (default: true)
    - `sentence_aware`: Use sentence boundaries (default: true)

    **Common Issues:**

    - If `chunk_overlap >= chunk_size`, raises RAGError
    - Empty or whitespace-only input returns empty list
    - Very long sentences may exceed chunk_size slightly to preserve completeness

    Args:
        text: The text to chunk (any length, any format)

    Returns:
        List of text chunks, each respecting semantic boundaries

    Raises:
        RAGError: If chunk configuration is invalid
    """

    cfg = _chunking_cfg()
    raw = (text or "").strip()
    if not raw:
        return []

    # Normalize newlines
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    # Phase 1: Extract heading-aware sections if enabled
    sections: list[tuple[str | None, str]] = []  # (heading, content)

    if cfg.preserve_headings:
        lines = raw.split("\n")
        current_heading: str | None = None
        current_content: list[str] = []

        for line in lines:
            if _is_heading(line):
                # Save previous section
                if current_content or current_heading:
                    content_text = "\n".join(current_content).strip()
                    if content_text or current_heading:
                        sections.append((current_heading, content_text))
                # Start new section
                current_heading = line
                current_content = []
            else:
                current_content.append(line)

        # Save final section
        if current_content or current_heading:
            content_text = "\n".join(current_content).strip()
            if content_text or current_heading:
                sections.append((current_heading, content_text))

        # If no headings found, treat whole text as one section
        if (
            not sections
        ):  # pragma: no cover - defensive: the loop above always appends at least one section when `raw` is non-empty
            sections = [(None, raw)]
    else:
        sections = [(None, raw)]

    # Phase 2: Build semantic chunks from sections
    def _process_text_unit(unit: str, max_size: int) -> list[str]:
        """Process a text unit (paragraph or sentence) into sub-chunks if needed."""
        if len(unit) <= max_size:
            return [unit]

        # If sentence-aware and unit is too long, try splitting into sentences
        if cfg.sentence_aware:
            sentences = _split_sentences(unit)
            if len(sentences) > 1:
                # Recursively chunk the sentences
                result: list[str] = []
                current: list[str] = []
                current_len = 0

                for sent in sentences:
                    sent_len = len(sent)
                    sep_len = 1 if current else 0  # space separator

                    if current and current_len + sep_len + sent_len > max_size:
                        # Flush current chunk
                        result.append(" ".join(current))
                        current = [sent]
                        current_len = sent_len
                    else:
                        current.append(sent)
                        current_len += sep_len + sent_len

                if current:
                    result.append(" ".join(current))
                return result

        # Fall back to word-safe wrapping for very long units
        words = unit.split()
        result = []
        current = []
        current_len = 0

        for word in words:
            word_len = len(word)
            sep_len = 1 if current else 0

            if current and current_len + sep_len + word_len > max_size:
                result.append(" ".join(current))
                current = [word]
                current_len = word_len
            else:
                current.append(word)
                current_len += sep_len + word_len

        if current:
            result.append(" ".join(current))
        return result

    chunks: list[str] = []

    for heading, content in sections:
        # Split content into paragraphs
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]

        # Build chunks from this section
        current_chunk: list[str] = []
        current_len = 0

        # Include heading in first chunk of section if present
        if heading:
            current_chunk.append(heading)
            current_len = len(heading) + 2  # +2 for \n\n separator

        for para in paragraphs:
            # Process paragraph into sub-chunks if needed
            para_chunks = _process_text_unit(para, cfg.chunk_size)

            for para_chunk in para_chunks:
                para_len = len(para_chunk)
                sep_len = 2 if current_chunk else 0  # \n\n separator

                # Check if adding this would exceed chunk_size
                if current_chunk and current_len + sep_len + para_len > cfg.chunk_size:
                    # Flush current chunk
                    chunks.append("\n\n".join(current_chunk))
                    current_chunk = [para_chunk]
                    current_len = para_len
                else:
                    current_chunk.append(para_chunk)
                    current_len += sep_len + para_len if current_len else para_len

        # Flush remaining content
        if current_chunk:
            chunks.append("\n\n".join(current_chunk))

    if not chunks:  # pragma: no cover - defensive: unreachable since `raw` is always non-empty here
        return []

    # Phase 3: Apply overlap strategy
    if cfg.overlap <= 0 or len(chunks) <= 1:
        return chunks

    if cfg.overlap_strategy == "sentence":
        # Overlap with complete sentences
        overlapped: list[str] = [chunks[0]]
        for i in range(1, len(chunks)):
            prev = overlapped[-1]
            # Extract sentences from previous chunk
            prev_sentences = _split_sentences(prev)
            # Find sentences that fit within overlap budget
            overlap_parts: list[str] = []
            overlap_len = 0
            for sent in reversed(prev_sentences):
                sent_len = len(sent)
                sep_len = 1 if overlap_parts else 0
                if overlap_len + sep_len + sent_len <= cfg.overlap:
                    overlap_parts.insert(0, sent)
                    overlap_len += sep_len + sent_len
                else:
                    break
            # Combine with current chunk
            if overlap_parts:
                overlap_text = " ".join(overlap_parts)
                combined = (overlap_text + "\n\n" + chunks[i]).strip()
            else:
                combined = chunks[i]
            overlapped.append(combined)
        return overlapped

    elif cfg.overlap_strategy == "semantic":
        # Overlap with complete paragraphs/sections
        overlapped = [chunks[0]]
        for i in range(1, len(chunks)):
            prev = overlapped[-1]
            # Extract paragraphs from previous chunk
            prev_paras = [p.strip() for p in prev.split("\n\n") if p.strip()]
            # Find paragraphs that fit within overlap budget
            overlap_parts = []
            overlap_len = 0
            for para in reversed(prev_paras):
                para_len = len(para)
                sep_len = 2 if overlap_parts else 0
                if overlap_len + sep_len + para_len <= cfg.overlap:
                    overlap_parts.insert(0, para)
                    overlap_len += sep_len + para_len
                else:
                    break
            # Combine with current chunk
            if overlap_parts:
                overlap_text = "\n\n".join(overlap_parts)
                combined = (overlap_text + "\n\n" + chunks[i]).strip()
            else:
                combined = chunks[i]
            overlapped.append(combined)
        return overlapped

    else:  # "trailing" strategy (legacy behavior)
        overlapped = [chunks[0]]
        for i in range(1, len(chunks)):
            prev = overlapped[-1]
            tail = prev[-cfg.overlap :]
            # Make overlap word-safe: start at next whitespace boundary
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
    *,
    collection: str = "default",
    embedding_model: str | None = None,
    embedding_dim: int | None = None,
    force_update: bool = False,
) -> dict:
    """Chunk, embed, and store a document in the vector store with update detection.

    This is the primary ingestion pipeline for adding documents to your
    RAG knowledge base. It handles all the complexity of chunking,
    embedding generation, storage, and automatic update detection.

    **Update Detection:**

    Uses SHA-256 content hashing to detect document changes:
    - If document doesn't exist, it's ingested
    - If document exists but content changed, stale chunks are deleted and new ones added
    - If document exists with same content, ingestion is skipped (unless force_update=True)

    **Multi-Model Support:**

    You can ingest documents using different embedding models by specifying
    a collection. Each collection uses its own table with the appropriate
    vector dimensions.

    **Pipeline Steps:**

    1. **Change Detection**: Compute document hash and check if update needed
    2. **Chunking**: Text is split into semantic chunks using paragraph-aware
       chunking with configurable overlap
    3. **Embedding**: Each chunk is converted to a vector embedding using
       the specified embedding model (or config default)
    4. **Cleanup**: For updates, delete stale chunks from previous version
    5. **Storage**: Chunks and embeddings are stored in Cassandra with
       version tracking metadata

    **Usage Examples:**

        # Basic document ingestion with automatic update detection
        >>> result = ingest_document(
        ...     doc_id="user-guide-v2.1",
        ...     text=documentation_text
        ... )
        >>> print(f"Status: {result['status']}, Chunks: {result['chunks_ingested']}")

        # Force re-ingestion even if content unchanged
        >>> result = ingest_document(
        ...     doc_id="api-reference",
        ...     text=api_docs,
        ...     force_update=True
        ... )

        # Use a specific embedding model (multi-model support)
        >>> result = ingest_document(
        ...     doc_id="api-reference",
        ...     text=api_docs,
        ...     collection="all-minilm",
        ...     embedding_model="all-minilm:latest",
        ...     embedding_dim=384
        ... )

        # With metadata for filtering
        >>> result = ingest_document(
        ...     doc_id="api-reference",
        ...     text=api_docs,
        ...     metadata={"type": "api", "version": "1.0"}
        ... )

        # First-time setup (creates schema)
        >>> result = ingest_document(
        ...     doc_id="first-doc",
        ...     text=content,
        ...     ensure_schema=True  # Only needed once per collection
        ... )

    **Important Notes:**

    - Documents with the same `doc_id` are replaced on content change
    - Empty text returns status "skipped" with 0 chunks
    - Embedding dimension is inferred from first embedding if ensure_schema=True
    - Cassandra connection is opened and closed within this function
    - Each collection maintains its own vector index with appropriate dimensions

    Args:
        doc_id: Unique identifier for this document (used for updates/deletes)
        text: The text content to ingest (any length)
        metadata: Optional metadata dict to attach to all chunks
        ensure_schema: If True, creates vector store schema if it doesn't exist
                      (should be True for first document, False afterwards)
        collection: Collection name for multi-model support (default: "default")
        embedding_model: Override embedding model (default: from config)
        embedding_dim: Override embedding dimension (default: from config)
        force_update: If True, re-ingest even if content hash matches (default: False)

    Returns:
        Dict with:
            - status: "ingested", "updated", or "skipped"
            - chunks_ingested: Number of chunks processed
            - doc_hash: Content hash of the document
            - previous_hash: Previous hash if it was an update (None otherwise)

    Raises:
        RAGError: If chunking configuration is invalid
        Exception: If Cassandra connection or embedding generation fails
    """
    if not text or not text.strip():
        return {
            "status": "skipped",
            "chunks_ingested": 0,
            "doc_hash": None,
            "previous_hash": None,
        }

    # Compute content hash for change detection
    doc_hash = compute_document_hash(text)

    start_time = time.perf_counter()
    store = CassandraVectorStore(collection=collection)
    try:
        if not ensure_schema and not store.schema_exists():
            # First-time ingest into this collection: create the schema
            # automatically instead of requiring callers to pass
            # ensure_schema=True out-of-band. Without this, a fresh install's
            # first ingest (ensure_schema defaults to False in the API/CLI)
            # would fail with "keyspace does not exist" instead of bootstrapping.
            ensure_schema = True

        if embedding_model is None:
            # No explicit override: fall back to the collection's configured
            # embedding model (set via POST /rag/collections or PUT
            # .../settings) so a configured setting actually affects what
            # ingestion embeds with, instead of always using the global default.
            stored_model = store.get_collection_settings().get("embedding_model")
            if isinstance(stored_model, str):
                embedding_model = stored_model

        if ensure_schema:
            # Need to chunk and embed first to infer dimension
            chunks = chunk_text(text)
            if not chunks:
                return {
                    "status": "skipped",
                    "chunks_ingested": 0,
                    "doc_hash": doc_hash,
                    "previous_hash": None,
                }

            embeddings_result = embed_texts(chunks, model=embedding_model, dimension=embedding_dim)
            embeddings: list[list[float]] = (
                embeddings_result if isinstance(embeddings_result, list) else embeddings_result[0]
            )

            # Get the actual model and dimension from embeddings config
            from nyxgpt.rag.embeddings import _embedding_cfg

            ecfg = _embedding_cfg(model=embedding_model, dimension=embedding_dim)
            actual_dim = len(embeddings[0]) if embeddings else ecfg.dimension
            store.ensure_schema(actual_dim, collection=collection)
        else:
            # Check if document needs update
            if not force_update and not store.document_needs_update(doc_id, doc_hash):
                log.info(f"Document {doc_id} unchanged, skipping re-ingestion")
                return {
                    "status": "skipped",
                    "chunks_ingested": 0,
                    "doc_hash": doc_hash,
                    "previous_hash": doc_hash,
                }

            # Document needs update - chunk and embed
            chunks = chunk_text(text)
            if not chunks:
                return {
                    "status": "skipped",
                    "chunks_ingested": 0,
                    "doc_hash": doc_hash,
                    "previous_hash": None,
                }

            embeddings_result = embed_texts(chunks, model=embedding_model, dimension=embedding_dim)
            embeddings = (
                embeddings_result if isinstance(embeddings_result, list) else embeddings_result[0]
            )

        # Get existing document info (including ingested_at for preservation)
        previous_hash = store.get_document_hash(doc_id)
        is_update = previous_hash is not None
        original_ingested_at = None

        # Delete stale chunks if this is an update (but preserve original ingested_at)
        if is_update:
            doc_info = store.get_document_info(doc_id)
            if doc_info and doc_info.get("ingested_at"):
                from datetime import datetime

                original_ingested_at = datetime.fromisoformat(doc_info["ingested_at"])
            log.info(f"Updating document {doc_id}: deleting stale chunks")
            store.delete_doc(doc_id)

        # Get the actual model and dimension from embeddings config
        from nyxgpt.rag.embeddings import _embedding_cfg

        ecfg = _embedding_cfg(model=embedding_model, dimension=embedding_dim)
        actual_model = ecfg.model
        actual_dim = len(embeddings[0]) if embeddings else ecfg.dimension

        metas = [metadata or {} for _ in chunks]

        # Upsert chunks with version tracking
        store.upsert_chunks(
            doc_id=doc_id,
            texts=chunks,
            embeddings=embeddings,
            metadatas=metas,
            embedding_model=actual_model,
            embedding_dim=actual_dim,
            doc_hash=doc_hash,
            original_ingested_at=original_ingested_at,
        )

        status = "updated" if is_update else "ingested"
        log.info(
            f"Document {doc_id} {status}: {len(chunks)} chunks",
            extra={
                "component": "rag",
                "doc_id": doc_id,
                "collection": collection,
                "status": status,
                "chunks_ingested": len(chunks),
                "duration_ms": round((time.perf_counter() - start_time) * 1000, 1),
            },
        )

        # Invalidate query result cache: the document set changed, so any
        # cached retrieval results may now be stale.
        clear_query_cache()

        return {
            "status": status,
            "chunks_ingested": len(chunks),
            "doc_hash": doc_hash,
            "previous_hash": previous_hash,
        }
    except Exception as e:
        log.error(
            "Document ingestion failed",
            extra={
                "component": "rag",
                "doc_id": doc_id,
                "collection": collection,
                "duration_ms": round((time.perf_counter() - start_time) * 1000, 1),
                "error": str(e),
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        raise
    finally:
        store.close()


# ----------------------------
# Query Expansion
# ----------------------------


def expand_query(query: str, max_expansions: int = 3) -> list[str]:
    """Generate alternative phrasings of a query using the local LLM.

    This improves retrieval quality by searching for multiple
    formulations of the same question. Uses the local Ollama
    instance so there's no external cost.

    Args:
        query: Original user query
        max_expansions: Maximum number of alternative phrasings to generate

    Returns:
        List containing original query plus expansions

    Example:
        >>> expand_query("How do I install Python?")
        [
            "How do I install Python?",
            "What are the steps to set up Python?",
            "Python installation guide"
        ]
    """
    cfg = load_config(None)

    # Check if query expansion is enabled
    if not cfg.getboolean("rag", "enable_query_expansion", fallback=False):
        return [query]

    try:
        from nyxgpt.config import get_default_model, get_ollama_base_url
        from nyxgpt.ollama_client import ollama_chat

        base_url = get_ollama_base_url(cfg)
        model = cfg.get("rag", "expansion_model", fallback=None) or get_default_model(cfg)

        system_prompt = (
            "You are a query expansion assistant. Given a search query, "
            "generate alternative phrasings that capture the same intent. "
            f"Return ONLY a JSON array of {max_expansions} alternative queries. "
            "Keep alternatives concise (under 100 chars each). "
            "Do NOT include the original query in your response."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Original query: {query}"},
        ]

        response = ollama_chat(
            base_url=base_url,
            model=model,
            messages=messages,
            timeout_s=10,  # Short timeout for expansion
            # Nothing reads this call's reasoning either, and 10s is below even
            # the *fast* default-mode sample (11.0s): with the shipped reasoning
            # model this call could only time out or burn budget (#4029 review).
            think=False,
        )

        # Parse JSON response
        # Strip markdown code blocks if present
        response = response.strip()
        if response.startswith("```json"):
            response = response[7:]
        if response.startswith("```"):
            response = response[3:]
        if response.endswith("```"):
            response = response[:-3]
        response = response.strip()

        expansions = json.loads(response)

        if isinstance(expansions, list):
            # Return original + valid expansions (up to max)
            valid = [str(e).strip() for e in expansions if e and len(str(e).strip()) > 0]
            return [query] + valid[:max_expansions]

    except Exception as e:
        log.warning(
            "Query expansion failed, using original query only: %s",
            e,
            extra={"component": "rag", "error_type": type(e).__name__},
        )

    return [query]


# ----------------------------
# Retrieve with Hybrid Search
# ----------------------------


def _execute_query_parallel(
    queries: list[str],
    query_embeddings: list[list[float]],
    store: CassandraVectorStore,
    k: int,
    actual_model: str,
    metadata_filter: MetadataFilter | None,
) -> tuple[dict[tuple, dict], list[float], int]:
    """Execute multiple vector searches as a single batched driver round trip.

    Uses :meth:`CassandraVectorStore.query_by_embeddings_batch`, which issues
    all ANN searches concurrently via the driver's native
    ``execute_concurrent`` instead of one blocking call per query
    variant, bounded by the store's configured ``cassandra_batch_query_concurrency``.

    Args:
        queries: List of query strings (for logging/debugging)
        query_embeddings: List of embedding vectors corresponding to queries
        store: Vector store instance to query
        k: Number of results to retrieve per query
        actual_model: Embedding model name for filtering results
        metadata_filter: Optional metadata filter to apply

    Returns:
        Tuple of (results_map, all_scores, total_raw_results):
        - results_map: Dict mapping (doc_id, chunk_id) -> result dict
        - all_scores: List of all similarity scores
        - total_raw_results: Total number of raw results before deduplication
    """
    results_map: dict[tuple, dict] = {}
    all_scores: list[float] = []

    batch_results = store.query_by_embeddings_batch(
        query_embeddings, k=k, embedding_model=actual_model, metadata_filter=metadata_filter
    )

    for idx, results in enumerate(batch_results):
        if not results:
            query_str = queries[idx] if idx < len(queries) else "unknown"
            log.debug("Vector search returned no results for query %d (%s)", idx, query_str)

        for r in results:
            text = (r.get("text") or "").strip()
            if not text:
                continue

            # Use (doc_id, chunk_id) as unique key
            chunk_key = (r.get("doc_id"), r.get("chunk_id"))
            if chunk_key not in results_map:
                results_map[chunk_key] = r

            # Collect score for statistics
            score = r.get("score")
            if score is not None:
                all_scores.append(float(score))

    total_raw_results = len(all_scores)  # Approximation: total results before dedup
    return results_map, all_scores, total_raw_results


@traced("rag.retrieve")
def retrieve_context(
    query: str,
    top_k: int | None = None,
    *,
    debug_mode: bool | None = None,
    collection: str = "default",
    embedding_model: str | None = None,
    embedding_dim: int | None = None,
    metadata_filter: MetadataFilter | None = None,
) -> list[dict] | tuple[list[dict], RAGDebugInfo]:
    """Retrieve relevant context for a query using hybrid search.

    Combines vector similarity search with BM25 keyword search using
    Reciprocal Rank Fusion (RRF) for improved retrieval quality.

    Optionally performs query expansion to improve recall.

    Multi-model support:
    - Specify collection to query a specific embedding model's vectors
    - Specify embedding_model to use a particular model for the query
    - Results are filtered by embedding_model to ensure compatibility

    Metadata filtering:
    - Filter results by doc_id, filename, tags, or date range
    - All filters are combined with AND logic
    - Filters are applied after vector search to preserve ranking quality

    Args:
        query: User's search query
        top_k: Number of results to return (uses config default if None)
        debug_mode: If True, return debug info; if None, use config setting
        collection: Collection name for multi-model support (default: "default")
        embedding_model: Override embedding model (default: from config)
        embedding_dim: Override embedding dimension (default: from config)
        metadata_filter: Optional metadata filter criteria (see MetadataFilter)

    Returns:
        List of result dictionaries with text, score, and metadata.
        If debug_mode=True, returns tuple of (results, RAGDebugInfo).
    """
    start_time = time.perf_counter()
    cfg = load_config(None)

    def _log_retrieval_completed(out_results: list[dict], *, cache_hit: bool) -> None:
        scores = [r["score"] for r in out_results if r.get("score") is not None]
        log.info(
            "RAG retrieval completed",
            extra={
                "component": "rag",
                "collection": collection,
                "top_k": k,
                "cache_hit": cache_hit,
                "result_count": len(out_results),
                "score_min": min(scores) if scores else None,
                "score_max": max(scores) if scores else None,
                "duration_ms": round((time.perf_counter() - start_time) * 1000, 1),
            },
        )

    # Determine debug mode from parameter or config
    collect_debug = debug_mode if debug_mode is not None else get_rag_debug_mode(cfg)

    # Get the actual model that will be used for embedding
    from nyxgpt.rag.embeddings import _embedding_cfg

    ecfg = _embedding_cfg(model=embedding_model, dimension=embedding_dim)
    actual_model = ecfg.model

    k = int(top_k) if top_k is not None else get_rag_chat_top_k(cfg)
    min_score = get_rag_min_score(cfg)
    max_chunks = get_rag_max_chunks(cfg)
    _dedupe = get_rag_dedupe(cfg)  # Reserved for future deduplication feature
    use_expansion = cfg.getboolean("rag", "enable_query_expansion", fallback=False)
    use_hybrid = cfg.getboolean("rag", "enable_hybrid_search", fallback=True)
    hybrid_alpha = cfg.getfloat("rag", "hybrid_alpha", fallback=None)
    reranking_enabled = cfg.getboolean("rag", "enable_reranking", fallback=False)

    # Query result cache: skip the entire retrieval pipeline on a hit.
    # Debug-mode calls are never cached/served from cache since they exist
    # to report live timing/metrics for that specific call.
    query_cache = _get_query_result_cache()
    cache_key = None
    if not collect_debug:
        cache_key = _query_cache_key(
            query=query,
            k=k,
            collection=collection,
            embedding_model=actual_model,
            embedding_dim=ecfg.dimension,
            metadata_filter=metadata_filter,
            min_score=min_score,
            max_chunks=max_chunks,
            use_expansion=use_expansion,
            use_hybrid=use_hybrid,
            hybrid_alpha=hybrid_alpha,
            reranking_enabled=reranking_enabled,
        )
        cached_results = query_cache.get(cache_key)
        if cached_results is not None:
            # Returned by reference and shared across all callers of this
            # cache key; callers must treat the result as read-only.
            log.debug(f"Query result cache hit for query={query!r}")
            _log_retrieval_completed(cached_results, cache_hit=True)
            return cached_results

    # Track debug metrics
    query_expansion_time_ms = None
    embedding_metrics: EmbeddingDebugMetrics | None = None
    vector_search_metrics: VectorSearchDebugMetrics | None = None
    keyword_search_time_ms = None
    fusion_time_ms = None
    keyword_results_count = None
    vector_results_count = None
    fusion_method = None

    # Generate query expansions if enabled
    if use_expansion and collect_debug:
        expansion_start = time.perf_counter()
        queries = expand_query(query)
        query_expansion_time_ms = (time.perf_counter() - expansion_start) * 1000.0
    else:
        queries = expand_query(query) if use_expansion else [query]

    if len(queries) > 1:
        log.debug("Expanded query into %d variants", len(queries))

    # ======================================================================
    # VECTOR SEARCH
    # ======================================================================
    vector_results_map: dict[tuple, dict] = {}  # (doc_id, chunk_id) -> result dict
    all_scores: list[float] = []
    total_raw_results = 0

    store = CassandraVectorStore(collection=collection)
    try:
        # Batch embed queries: use embed_texts for multiple queries, embed_text for single
        if collect_debug:
            result = embed_texts(
                queries, collect_metrics=True, model=embedding_model, dimension=embedding_dim
            )
            query_embeddings, emb_metrics = cast(
                tuple[list[list[float]], EmbeddingDebugMetrics], result
            )
            embedding_metrics = emb_metrics
        elif len(queries) > 1:
            # Use embed_texts for batching multiple queries (reduces API calls)
            query_embeddings = cast(
                list[list[float]],
                embed_texts(
                    queries, collect_metrics=False, model=embedding_model, dimension=embedding_dim
                ),
            )
        else:
            # Single query: use embed_text (for test compatibility)
            query_embeddings = [
                embed_text(queries[0], model=embedding_model, dimension=embedding_dim)
            ]

        # Batched execution for multiple queries (performance optimization)
        if len(queries) > 1 and not collect_debug:
            log.debug("Executing %d queries via batched ANN search", len(queries))

            # Execute vector searches as a single batched round trip
            vector_results_map, all_scores, total_raw_results = _execute_query_parallel(
                queries, query_embeddings, store, k, actual_model, metadata_filter
            )
        else:
            # Sequential execution for single query or debug mode
            for idx, q_emb in enumerate(query_embeddings):
                # Query vector store with metrics collection if debug mode
                if collect_debug:
                    vs_result = store.query_by_embedding(
                        q_emb,
                        k=k,
                        collect_metrics=True,
                        embedding_model=actual_model,
                        metadata_filter=metadata_filter,
                    )
                    # Type narrowing: vs_result is tuple[list[dict], VectorSearchDebugMetrics]
                    results, vs_metrics = cast(
                        tuple[list[dict], VectorSearchDebugMetrics], vs_result
                    )
                    total_raw_results += vs_metrics.raw_results_count
                    # Accumulate scores for overall statistics
                    if vs_metrics.score_min is not None:
                        all_scores.extend([r.get("score", 0.0) for r in results])
                    # Store metrics from first query
                    if idx == 0:
                        vector_search_metrics = vs_metrics
                else:
                    results = cast(
                        list[dict],
                        store.query_by_embedding(
                            q_emb,
                            k=k,
                            embedding_model=actual_model,
                            metadata_filter=metadata_filter,
                        ),
                    )

                for r in results:
                    text = (r.get("text") or "").strip()
                    if not text:
                        continue

                    # Use (doc_id, chunk_id) as unique key for deduplication
                    chunk_key = (r.get("doc_id"), r.get("chunk_id"))
                    if chunk_key not in vector_results_map:
                        vector_results_map[chunk_key] = r

        # Get list of all documents for keyword search
        all_docs = store.list_docs()
        if not all_docs:
            log.debug("No documents in vector store, skipping hybrid search")
            use_hybrid = False
    finally:
        # Don't close store yet if we need to fetch documents for BM25
        pass

    # ======================================================================
    # KEYWORD SEARCH (BM25)
    # ======================================================================
    keyword_results_map: dict[tuple, dict] = {}  # (doc_id, chunk_id) -> result dict

    if use_hybrid and all_docs:
        keyword_start = time.perf_counter()

        # Fetch all document chunks for BM25 indexing
        # Build a mapping: chunk_idx -> result dict
        all_chunks: list[str] = []
        chunk_to_result: dict[int, dict] = {}
        fetch_failed_count = 0

        for doc_info in all_docs:
            doc_id = doc_info["doc_id"]

            # Apply metadata filter to doc_id (skip entire document if filtered out)
            if (
                metadata_filter
                and metadata_filter.doc_ids
                and doc_id not in metadata_filter.doc_ids
            ):
                continue

            # Fetch all chunks for this document
            try:
                # Query by doc_id to get all chunks (include ingested_at for date filtering)
                stmt = f"SELECT doc_id, chunk_id, text, metadata, embedding_model, embedding_dim, ingested_at FROM {store.table_name} WHERE doc_id = %s"
                if not store._keyspace_ready:
                    store._ensure_keyspace_selected()
                rows = store.session.execute(stmt, (doc_id,))

                for r in rows:
                    # Filter by embedding_model if specified
                    if (
                        actual_model is not None
                        and hasattr(r, "embedding_model")
                        and r.embedding_model != actual_model
                    ):
                        continue

                    # Parse metadata for filtering
                    metadata = parse_metadata(r.metadata, doc_id=doc_id)

                    # Apply metadata filters (same logic as in query_by_embedding)
                    if metadata_filter:
                        # Filter by filename (case-insensitive partial match)
                        if metadata_filter.filename:
                            doc_filename = metadata.get("filename", "")
                            if metadata_filter.filename.lower() not in doc_filename.lower():
                                continue

                        # Filter by tags (doc must have ALL specified tags)
                        if metadata_filter.tags:
                            doc_tags = metadata.get("tags", [])
                            if not isinstance(doc_tags, list):
                                continue
                            if not all(tag in doc_tags for tag in metadata_filter.tags):
                                continue

                        # Filter by date range
                        if metadata_filter.date_from or metadata_filter.date_to:
                            if not hasattr(r, "ingested_at") or r.ingested_at is None:
                                continue
                            if (
                                metadata_filter.date_from
                                and r.ingested_at < metadata_filter.date_from
                            ):
                                continue
                            if metadata_filter.date_to and r.ingested_at > metadata_filter.date_to:
                                continue

                    text = (r.text or "").strip()
                    if not text:
                        continue

                    chunk_idx = len(all_chunks)
                    all_chunks.append(text)
                    chunk_to_result[chunk_idx] = {
                        "doc_id": r.doc_id,
                        "chunk_id": r.chunk_id,
                        "text": text,
                        "metadata": metadata,
                        "embedding_model": (
                            r.embedding_model if hasattr(r, "embedding_model") else None
                        ),
                        "embedding_dim": r.embedding_dim if hasattr(r, "embedding_dim") else None,
                    }
            except Exception as e:
                log.warning("Failed to fetch chunks for doc %s: %s", doc_id, e)
                fetch_failed_count += 1
                continue

        if fetch_failed_count:
            log.warning(
                "BM25 chunk fetch had failures",
                extra={
                    "component": "rag",
                    "docs_total": len(all_docs),
                    "docs_failed": fetch_failed_count,
                },
            )

        # Build BM25 index and search
        if all_chunks:
            bm25_index = BM25Index()
            bm25_index.build_index(all_chunks)
            bm25_results = bm25_index.search(query, k=k)

            # Convert BM25 results to result dict format
            for chunk_idx, bm25_score in bm25_results:
                if chunk_idx in chunk_to_result:
                    chunk_result = chunk_to_result[chunk_idx].copy()
                    chunk_result["score"] = bm25_score
                    chunk_key = (chunk_result["doc_id"], chunk_result["chunk_id"])
                    keyword_results_map[chunk_key] = chunk_result

        keyword_search_time_ms = (time.perf_counter() - keyword_start) * 1000.0
        keyword_results_count = len(keyword_results_map)

    # Close store after we're done with Cassandra operations
    store.close()

    # ======================================================================
    # FUSION
    # ======================================================================
    vector_results_count = len(vector_results_map)

    if use_hybrid and keyword_results_map:
        fusion_start = time.perf_counter()

        # Prepare rankings for fusion
        # Vector results: sorted by score descending
        vector_ranking = sorted(
            [(k, r["score"]) for k, r in vector_results_map.items()],
            key=lambda x: x[1],
            reverse=True,
        )

        # Keyword results: sorted by score descending
        keyword_ranking = sorted(
            [(k, r["score"]) for k, r in keyword_results_map.items()],
            key=lambda x: x[1],
            reverse=True,
        )

        # Decide fusion method
        if hybrid_alpha is not None:
            # Use weighted fusion
            fusion_method = f"weighted(alpha={hybrid_alpha})"
            fused_ranking = weighted_fusion(vector_ranking, keyword_ranking, alpha=hybrid_alpha)
        else:
            # Use RRF (default)
            fusion_method = "reciprocal_rank_fusion"
            fused_ranking = reciprocal_rank_fusion([vector_ranking, keyword_ranking])

        # Build final results from fused ranking
        all_results: list[dict] = []
        for chunk_key, fused_score in fused_ranking:
            # Get result from either vector or keyword results
            if chunk_key in vector_results_map:
                fused_result = vector_results_map[chunk_key].copy()
                # Preserve original vector similarity score for UI display
                fused_result["similarity_score"] = fused_result.get("score")
            else:
                # Keyword-only result - no vector similarity available
                fused_result = keyword_results_map[chunk_key].copy()
                fused_result["similarity_score"] = None

            # Update score to fused score (used for ranking)
            fused_result["score"] = fused_score
            all_results.append(fused_result)

        fusion_time_ms = (time.perf_counter() - fusion_start) * 1000.0
    else:
        # No hybrid search, use vector results only
        fusion_method = "vector_only"
        all_results = sorted(
            vector_results_map.values(), key=lambda r: r.get("score", 0.0), reverse=True
        )

    # ======================================================================
    # RERANKING
    # ======================================================================
    reranking_metrics: RerankerDebugMetrics | None = None

    if reranking_enabled and all_results:
        log.debug("Applying reranking to %d results", len(all_results))
        if collect_debug:
            rerank_result = rerank_results(query, all_results, collect_metrics=True)
            # Type narrowing: collect_metrics=True returns tuple
            assert isinstance(rerank_result, tuple), "Expected tuple when collect_metrics=True"
            reranked_results, reranking_metrics = rerank_result
            all_results = reranked_results
        else:
            rerank_result = rerank_results(query, all_results)
            # Type narrowing: collect_metrics=False returns list
            assert isinstance(rerank_result, list), "Expected list when collect_metrics=False"
            all_results = rerank_result

    # ======================================================================
    # FILTERING
    # ======================================================================
    filter_start = time.perf_counter() if collect_debug else None
    after_dedupe = len(all_results)

    filtered: list[dict] = []
    for r in all_results:
        score = r.get("score")
        if score is not None and score < min_score:
            continue

        filtered.append(r)
        if len(filtered) >= max_chunks:
            break

    after_min_score = len(filtered)
    after_max_chunks = len(filtered)

    filtering_time_ms = (time.perf_counter() - filter_start) * 1000.0 if filter_start else 0.0

    if not collect_debug:
        if cache_key is not None:
            query_cache.set(cache_key, filtered)
        _log_retrieval_completed(filtered, cache_hit=False)
        return filtered

    # Build debug info
    total_time_ms = (time.perf_counter() - start_time) * 1000.0

    # Calculate overall score statistics if we have multiple queries
    if len(queries) > 1 and all_scores:
        overall_score_min = min(all_scores) if all_scores else None
        overall_score_max = max(all_scores) if all_scores else None
        overall_score_mean = sum(all_scores) / len(all_scores) if all_scores else None
    else:
        overall_score_min = vector_search_metrics.score_min if vector_search_metrics else None
        overall_score_max = vector_search_metrics.score_max if vector_search_metrics else None
        overall_score_mean = vector_search_metrics.score_mean if vector_search_metrics else None

    debug_info = RAGDebugInfo(
        total_time_ms=total_time_ms,
        query_expansion_time_ms=query_expansion_time_ms,
        embedding_time_ms=embedding_metrics.embedding_time_ms if embedding_metrics else 0.0,
        vector_search_time_ms=(
            vector_search_metrics.vector_search_time_ms if vector_search_metrics else 0.0
        ),
        keyword_search_time_ms=keyword_search_time_ms,
        fusion_time_ms=fusion_time_ms,
        reranking_time_ms=reranking_metrics.reranking_time_ms if reranking_metrics else None,
        filtering_time_ms=filtering_time_ms,
        composition_time_ms=0.0,  # compose_context is called separately
        original_query=query,
        query_variants=queries,
        num_queries=len(queries),
        embedding_model=embedding_metrics.embedding_model if embedding_metrics else "",
        embedding_dim=embedding_metrics.embedding_dim if embedding_metrics else 0,
        num_texts_embedded=embedding_metrics.num_texts_embedded if embedding_metrics else 0,
        batch_size=embedding_metrics.batch_size if embedding_metrics else 0,
        raw_results_count=(
            total_raw_results
            if len(queries) > 1
            else (vector_search_metrics.raw_results_count if vector_search_metrics else 0)
        ),
        score_min=overall_score_min,
        score_max=overall_score_max,
        score_mean=overall_score_mean,
        hybrid_enabled=use_hybrid,
        keyword_results_count=keyword_results_count,
        vector_results_count=vector_results_count,
        fusion_method=fusion_method,
        reranking_enabled=reranking_enabled,
        reranker_model=reranking_metrics.reranker_model if reranking_metrics else None,
        num_candidates_reranked=reranking_metrics.num_candidates if reranking_metrics else None,
        num_results_after_rerank=reranking_metrics.num_reranked if reranking_metrics else None,
        after_min_score_filter=after_min_score,
        after_dedupe_filter=after_dedupe,
        after_max_chunks_filter=after_max_chunks,
        total_chars_before_truncation=0,  # Not available at this level
        total_chars_after_truncation=0,  # Not available at this level
        chunks_included=len(filtered),
        collection=collection,
    )

    _log_retrieval_completed(filtered, cache_hit=False)
    return filtered, debug_info


def annotate_chunk_numbering(rows: list[dict], *, collection: str = "default") -> list[dict]:
    """Stamp human-readable chunk position and source collection onto retrieval rows.

    Mutates each row in place, adding:
    - ``collection``: the collection the row was retrieved from
    - ``chunk_number``: 1-based chunk position (the raw ``chunk_id`` is a
      zero-based Cassandra clustering key, not meant for display), when
      ``chunk_id`` is present and numeric
    - ``total_chunks``: total chunk count for that row's document, or None
      if the lookup fails or a doc lookup errors (citation display should
      never fail a chat/query response just because this best-effort count
      is unavailable)

    Looks up each unique ``doc_id`` at most once via ``get_document_info``.

    Args:
        rows: Retrieval result dicts, each with ``doc_id`` and ``chunk_id``
        collection: Collection all rows were retrieved from

    Returns:
        The same list, with rows mutated in place
    """
    if not rows:
        return rows

    for row in rows:
        row["collection"] = collection
        chunk_id = row.get("chunk_id")
        if chunk_id is not None:
            with contextlib.suppress(TypeError, ValueError):
                row["chunk_number"] = int(chunk_id) + 1

    store = None
    total_chunks_by_doc: dict[str, int | None] = {}
    try:
        store = CassandraVectorStore(collection=collection)
        for row in rows:
            doc_id = row.get("doc_id")
            if doc_id is None:
                continue
            if doc_id not in total_chunks_by_doc:
                info = store.get_document_info(doc_id)
                total_chunks_by_doc[doc_id] = info["chunks"] if info else None
            row["total_chunks"] = total_chunks_by_doc[doc_id]
    except Exception:
        log.warning("Failed to look up total chunk counts for citations", exc_info=True)
    finally:
        if store is not None:
            store.close()
    return rows


def compute_evaluation_metrics(
    results: list[dict],
    debug_info: RAGDebugInfo,
    min_score: float,
) -> RAGEvaluationMetrics:
    """Compute comprehensive evaluation metrics from retrieval results.

    Args:
        results: Retrieved results from retrieve_context
        debug_info: Debug information from retrieve_context
        min_score: Minimum score threshold for filtering

    Returns:
        RAGEvaluationMetrics with accuracy, latency, and hit rate metrics
    """
    # Extract scores for distribution analysis
    scores = [r.get("score", 0.0) for r in results if r.get("score") is not None]

    # Compute score percentiles
    score_distribution = {}
    if scores:
        sorted_scores = sorted(scores)
        score_distribution = {
            "p50": statistics.median(sorted_scores),
            "p75": (
                statistics.quantiles(sorted_scores, n=4)[2]
                if len(sorted_scores) >= 2
                else sorted_scores[-1]
            ),
            "p95": (
                statistics.quantiles(sorted_scores, n=20)[18]
                if len(sorted_scores) >= 2
                else sorted_scores[-1]
            ),
            "p99": (
                statistics.quantiles(sorted_scores, n=100)[98]
                if len(sorted_scores) >= 2
                else sorted_scores[-1]
            ),
        }

    # Retrieval accuracy metrics
    unique_docs = len({r.get("doc_id") for r in results if r.get("doc_id")})
    retrieval_accuracy = RetrievalAccuracyMetrics(
        results_returned=len(results),
        query_success=len(results) > 0,
        unique_docs_retrieved=unique_docs,
        total_chunks_retrieved=len(results),
        score_distribution=score_distribution,
    )

    # Latency metrics with stage breakdowns
    stage_timings = {
        "query_expansion": debug_info.query_expansion_time_ms or 0.0,
        "embedding": debug_info.embedding_time_ms,
        "vector_search": debug_info.vector_search_time_ms,
        "filtering": debug_info.filtering_time_ms,
        "composition": debug_info.composition_time_ms,
    }
    latency = LatencyMetrics(
        total_time_ms=debug_info.total_time_ms,
        stage_timings=stage_timings,
        percentiles=None,  # Computed from historical data (not available in single query)
    )

    # Hit rate metrics
    successful = 1 if len(results) > 0 else 0
    failed = 1 - successful
    query_success_rate = float(successful)  # For single query, rate is 0.0 or 1.0

    # Score statistics
    avg_top_score = scores[0] if scores else None
    above_threshold = sum(1 for s in scores if s >= min_score)
    score_above_threshold_rate = float(above_threshold) / len(scores) if scores else 0.0

    hit_rate = HitRateMetrics(
        query_success_rate=query_success_rate,
        total_queries=1,
        successful_queries=successful,
        failed_queries=failed,
        avg_top_score=avg_top_score,
        score_above_threshold_rate=score_above_threshold_rate,
    )

    # Generate unique query ID and timestamp
    query_id = str(uuid.uuid4())
    timestamp = time.time()

    return RAGEvaluationMetrics(
        retrieval_accuracy=retrieval_accuracy,
        latency=latency,
        hit_rate=hit_rate,
        query_id=query_id,
        timestamp=timestamp,
    )


# ----------------------------
# Compose
# ----------------------------


def compose_context(results: Iterable[dict]) -> str:
    """Format retrieved chunks into context for LLM injection.

    Takes raw retrieval results and formats them into a readable context
    block that can be injected into the LLM's system prompt. Handles
    header formatting, score inclusion, and character limiting.

    **Output Format:**

    With headers enabled (default):
    ```
    [Context 1] (score=0.876)
    First relevant chunk text here...

    [Context 2] (score=0.834)
    Second relevant chunk text here...
    ```

    Without headers:
    ```
    First relevant chunk text here...

    Second relevant chunk text here...
    ```

    **Configuration:**

    - `chat_context_max_chars`: Maximum total characters (default: 2400)
    - `include_scores`: Show similarity scores (default: false)
    - `include_headers`: Show [Context N] labels (default: true)

    **Character Limiting:**

    If total context would exceed max_chars, chunks are:
    1. Included in order until limit reached
    2. Last chunk truncated to fit within limit
    3. Remaining chunks discarded

    Args:
        results: Iterable of result dicts from retrieve_context()

    Returns:
        Formatted context string ready for LLM injection
    """
    cfg = load_config(None)
    max_chars = get_rag_chat_context_max_chars(cfg)
    include_scores = get_rag_include_scores(cfg)
    include_headers = get_rag_include_headers(cfg)

    parts: list[str] = []
    used = 0

    for i, r in enumerate(results, start=1):
        text = (r.get("text") or "").strip()
        if not text:
            continue

        header = ""
        if include_headers:
            header = f"[Context {i}]"
            if include_scores and r.get("score") is not None:
                header += f" (score={r.get('score'):.3f})"
            header += "\n"

        block = (header + text).strip()

        if max_chars > 0:
            remaining = max_chars - used
            if (
                remaining <= 0
            ):  # pragma: no cover - defensive: the loop always breaks below once `used` reaches `max_chars`, so `remaining` can't go negative on a later iteration
                break
            if len(block) > remaining:
                block = block[:remaining].rstrip()

        parts.append(block)
        used += len(block) + 2

        if max_chars > 0 and used >= max_chars:
            break

    return "\n\n".join(parts)


def ingest_repository(
    repo_path: str,
    doc_id_prefix: str = "code",
    extensions: set[str] | None = None,
    extract_docs_only: bool = False,
    ensure_schema: bool = False,
    collection: str = "default",
    embedding_model: str | None = None,
    embedding_dim: int | None = None,
) -> dict:
    """Ingest a code repository for RAG.

    Args:
        repo_path: Path to the repository root
        doc_id_prefix: Prefix for document IDs (default: "code")
        extensions: Set of file extensions to include (e.g., {'.py', '.js'}). If None, use all supported.
        extract_docs_only: If True, extract only comments/docstrings. If False, include full code.
        ensure_schema: If True, create schema if it doesn't exist
        collection: Target collection name
        embedding_model: Override embedding model
        embedding_dim: Override embedding dimension

    Returns:
        Dictionary with ingestion results:
            - 'total_files': number of files indexed
            - 'total_chunks': total chunks ingested
            - 'files': list of indexed file paths
            - 'doc_ids': list of document IDs created
    """
    from pathlib import Path

    from nyxgpt.rag.code_parser import index_repository

    repo_path_obj = Path(repo_path).resolve()
    if not repo_path_obj.exists():
        raise ValueError(f"Repository path does not exist: {repo_path}")
    if not repo_path_obj.is_dir():
        raise ValueError(f"Repository path is not a directory: {repo_path}")

    # Security: Validate repo path is within allowed directories
    # Restrict to user home directory, current working directory, or trusted paths
    import os

    allowed_base_paths = [
        Path.home(),
        Path.cwd(),
    ]

    # Add trusted paths from environment variable (colon-separated)
    # Example: export NYXGPT_TRUSTED_PATHS="/opt/repos:/usr/local/repos"
    trusted_paths_env = os.environ.get("NYXGPT_TRUSTED_PATHS", "").strip()
    if trusted_paths_env:
        for trusted_path in trusted_paths_env.split(":"):
            if trusted_path:
                allowed_base_paths.append(Path(trusted_path).resolve())

    # Check if repo_path is within allowed directories
    is_allowed = False
    for base_path in allowed_base_paths:
        try:
            repo_path_obj.relative_to(base_path.resolve())
            is_allowed = True
            break
        except ValueError:
            continue

    if not is_allowed:
        allowed_paths_str = ", ".join(str(p) for p in allowed_base_paths)
        raise ValueError(
            f"Repository path is outside allowed directories. "
            f"Allowed paths: {allowed_paths_str}. "
            f"To add more trusted paths, set NYXGPT_TRUSTED_PATHS environment variable."
        )

    log.info(f"Indexing repository: {repo_path}")
    log.info(f"  Extensions: {extensions or 'all supported'}")
    log.info(f"  Extract docs only: {extract_docs_only}")

    # Index the repository
    result = index_repository(
        repo_path_obj,
        extensions=extensions,
        extract_docs_only=extract_docs_only,
        max_chunk_size=800,  # Use standard chunk size
    )

    files = result["files"]
    chunks = result["chunks"]
    total_chunks = result["total_chunks"]

    log.info(f"Found {len(files)} files, {total_chunks} chunks")

    # Ingest each file as a separate document
    doc_ids = []
    total_ingested = 0

    for file_path_str in files:
        # Create document ID from file path
        file_path_obj = Path(file_path_str)
        try:
            relative_path = file_path_obj.relative_to(repo_path_obj)
        except ValueError:
            relative_path = file_path_obj

        doc_id = f"{doc_id_prefix}:{str(relative_path).replace('/', ':')}"
        doc_ids.append(doc_id)

        # Collect all chunks for this file
        file_chunks = [chunk for fp, idx, chunk in chunks if fp == file_path_str]
        if not file_chunks:
            continue

        # Combine chunks into single text for this file
        combined_text = "\n\n".join(file_chunks)

        # Ingest the document
        metadata = {
            "file_path": str(relative_path),
            "repo_path": str(repo_path_obj),
            "language": file_path_obj.suffix.lstrip("."),
            "extract_docs_only": extract_docs_only,
        }

        ingest_result = ingest_document(
            doc_id,
            combined_text,
            metadata=metadata,
            ensure_schema=ensure_schema,
            collection=collection,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
        )

        total_ingested += ingest_result["chunks_ingested"]
        log.info(
            f"  Ingested {doc_id}: {ingest_result['chunks_ingested']} chunks ({ingest_result['status']})"
        )

    log.info(f"Repository ingestion complete: {len(doc_ids)} files, {total_ingested} total chunks")

    return {
        "total_files": len(doc_ids),
        "total_chunks": total_ingested,
        "files": files,
        "doc_ids": doc_ids,
    }
