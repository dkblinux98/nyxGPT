from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List
import logging
import json
import time

from mygpt.config import (
    load_config,
    get_rag_chat_top_k,
    get_rag_min_score,
    get_rag_max_chunks,
    get_rag_chat_context_max_chars,
    get_rag_dedupe,
    get_rag_include_scores,
    get_rag_include_headers,
    get_rag_debug_mode,
)
from mygpt.rag.embeddings import embed_text, embed_texts, EmbeddingDebugMetrics
from mygpt.rag.vectorstore_cassandra import CassandraVectorStore, VectorSearchDebugMetrics

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChunkingConfig:
    chunk_size: int
    overlap: int


@dataclass
class RAGDebugInfo:
    """Complete debug information for RAG operations."""
    # Timing
    total_time_ms: float
    query_expansion_time_ms: float | None
    embedding_time_ms: float
    vector_search_time_ms: float
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

    # Filtering stats
    after_min_score_filter: int
    after_dedupe_filter: int
    after_max_chunks_filter: int

    # Context composition
    total_chars_before_truncation: int
    total_chars_after_truncation: int
    chunks_included: int


class RAGError(RuntimeError):
    pass


def _chunking_cfg() -> ChunkingConfig:
    cfg = load_config(None)
    size = cfg.getint("rag", "chunk_size", fallback=800)
    overlap = cfg.getint("rag", "chunk_overlap", fallback=100)
    if overlap >= size:
        raise RAGError("chunk_overlap must be smaller than chunk_size")
    return ChunkingConfig(chunk_size=size, overlap=overlap)


# ----------------------------
# Chunking
# ----------------------------


def chunk_text(text: str) -> list[str]:
    """Chunk text in a word-safe, paragraph-aware way.

    This function implements a sophisticated text chunking algorithm designed
    to maximize the quality of retrieval-augmented generation (RAG) results.
    Unlike naive character-based chunking, this approach preserves semantic
    boundaries and ensures chunks are human-readable.

    **Why This Approach?**

    1. **Semantic Preservation**: Splitting on paragraph boundaries keeps
       related ideas together, improving retrieval relevance.

    2. **Readability**: Word-safe cutting means retrieved chunks don't have
       mid-word breaks like "The quick brown f" which confuse both humans
       and LLMs.

    3. **Context Continuity**: Overlap between chunks means even if the most
       relevant sentence is at a chunk boundary, surrounding context is
       preserved.

    **Algorithm Overview:**

    Phase 1 - Paragraph-based chunking:
      - Split on blank lines (paragraph boundaries).
      - Build chunks by concatenating paragraphs until `chunk_size` would be exceeded.
      - For paragraphs longer than `chunk_size`, fall back to word-safe wrapping.

    Phase 2 - Overlap application:
      - Apply `chunk_overlap` as an overlap of trailing characters (word-safe) between chunks.
      - Ensures overlap starts at word boundary for readability.

    **Configuration:**

    Controlled by `[rag]` section in config.ini:
    - `chunk_size`: Target maximum characters per chunk (default: 800)
    - `chunk_overlap`: Characters to overlap between chunks (default: 100)

    **Common Issues:**

    - If `chunk_overlap >= chunk_size`, raises RAGError
    - Empty or whitespace-only input returns empty list
    - Very long paragraphs are word-wrapped to fit chunk_size

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
    *,
    collection: str = "default",
    embedding_model: str | None = None,
    embedding_dim: int | None = None,
) -> int:
    """Chunk, embed, and store a document in the vector store.

    This is the primary ingestion pipeline for adding documents to your
    RAG knowledge base. It handles all the complexity of chunking,
    embedding generation, and storage.

    **Multi-Model Support:**

    You can ingest documents using different embedding models by specifying
    a collection. Each collection uses its own table with the appropriate
    vector dimensions.

    **Pipeline Steps:**

    1. **Chunking**: Text is split into semantic chunks using paragraph-aware
       chunking with configurable overlap.

    2. **Embedding**: Each chunk is converted to a vector embedding using
       the specified embedding model (or config default).

    3. **Storage**: Chunks and embeddings are stored in Cassandra with
       upsert semantics (existing doc_id is replaced).

    **Usage Examples:**

        # Basic document ingestion (uses default collection)
        >>> n = ingest_document(
        ...     doc_id="user-guide-v2.1",
        ...     text=documentation_text
        ... )
        >>> print(f"Ingested {n} chunks")

        # Use a specific embedding model (multi-model support)
        >>> n = ingest_document(
        ...     doc_id="api-reference",
        ...     text=api_docs,
        ...     collection="all-minilm",
        ...     embedding_model="all-minilm:latest",
        ...     embedding_dim=384
        ... )

        # With metadata for filtering
        >>> n = ingest_document(
        ...     doc_id="api-reference",
        ...     text=api_docs,
        ...     metadata={"type": "api", "version": "1.0"}
        ... )

        # First-time setup (creates schema)
        >>> n = ingest_document(
        ...     doc_id="first-doc",
        ...     text=content,
        ...     ensure_schema=True  # Only needed once per collection
        ... )

    **Important Notes:**

    - Documents with the same `doc_id` are replaced (upsert semantics)
    - Empty text returns 0 without error
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

    Returns:
        Number of chunks successfully ingested

    Raises:
        RAGError: If chunking configuration is invalid
        Exception: If Cassandra connection or embedding generation fails
    """
    chunks = chunk_text(text)
    if not chunks:
        return 0

    embeddings = embed_texts(chunks, model=embedding_model, dimension=embedding_dim)

    # Get the actual model and dimension from embeddings config
    from mygpt.rag.embeddings import _embedding_cfg
    ecfg = _embedding_cfg(model=embedding_model, dimension=embedding_dim)
    actual_model = ecfg.model
    actual_dim = len(embeddings[0]) if embeddings else ecfg.dimension

    metas = [metadata or {} for _ in chunks]

    store = CassandraVectorStore(collection=collection)
    try:
        if ensure_schema:
            # embedding dimension inferred from first vector
            store.ensure_schema(actual_dim, collection=collection)

        store.upsert_chunks(
            doc_id=doc_id,
            texts=chunks,
            embeddings=embeddings,
            metadatas=metas,
            embedding_model=actual_model,
            embedding_dim=actual_dim,
        )
    finally:
        store.close()

    return len(chunks)


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
        from mygpt.config import get_ollama_base_url, get_default_model
        from mygpt.ollama_client import ollama_chat

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
            {"role": "user", "content": f"Original query: {query}"}
        ]

        response = ollama_chat(
            base_url=base_url,
            model=model,
            messages=messages,
            timeout_s=10  # Short timeout for expansion
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
        log.warning("Query expansion failed, using original query only: %s", e)

    return [query]


# ----------------------------
# Retrieve with Hybrid Search
# ----------------------------


def retrieve_context(
    query: str,
    top_k: int | None = None,
    *,
    debug_mode: bool | None = None,
    collection: str = "default",
    embedding_model: str | None = None,
    embedding_dim: int | None = None,
) -> list[dict] | tuple[list[dict], RAGDebugInfo]:
    """Retrieve relevant context for a query.

    Optionally performs query expansion to improve recall.

    Multi-model support:
    - Specify collection to query a specific embedding model's vectors
    - Specify embedding_model to use a particular model for the query
    - Results are filtered by embedding_model to ensure compatibility

    Args:
        query: User's search query
        top_k: Number of results to return (uses config default if None)
        debug_mode: If True, return debug info; if None, use config setting
        collection: Collection name for multi-model support (default: "default")
        embedding_model: Override embedding model (default: from config)
        embedding_dim: Override embedding dimension (default: from config)

    Returns:
        List of result dictionaries with text, score, and metadata.
        If debug_mode=True, returns tuple of (results, RAGDebugInfo).
    """
    start_time = time.perf_counter()
    cfg = load_config(None)

    # Determine debug mode from parameter or config
    collect_debug = debug_mode if debug_mode is not None else get_rag_debug_mode(cfg)

    # Get the actual model that will be used for embedding
    from mygpt.rag.embeddings import _embedding_cfg
    ecfg = _embedding_cfg(model=embedding_model, dimension=embedding_dim)
    actual_model = ecfg.model

    k = int(top_k) if top_k is not None else get_rag_chat_top_k(cfg)
    min_score = get_rag_min_score(cfg)
    max_chunks = get_rag_max_chunks(cfg)
    _dedupe = get_rag_dedupe(cfg)  # Reserved for future deduplication feature
    use_expansion = cfg.getboolean("rag", "enable_query_expansion", fallback=False)

    # Track debug metrics
    query_expansion_time_ms = None
    embedding_metrics: EmbeddingDebugMetrics | None = None
    vector_search_metrics: VectorSearchDebugMetrics | None = None

    # Generate query expansions if enabled
    if use_expansion and collect_debug:
        expansion_start = time.perf_counter()
        queries = expand_query(query)
        query_expansion_time_ms = (time.perf_counter() - expansion_start) * 1000.0
    else:
        queries = expand_query(query) if use_expansion else [query]

    if len(queries) > 1:
        log.debug("Expanded query into %d variants", len(queries))

    # Collect results from all query variants
    all_results: list[dict] = []
    seen_texts: set[str] = set()
    total_raw_results = 0
    all_scores: list[float] = []

    store = CassandraVectorStore(collection=collection)
    try:
        for idx, q in enumerate(queries):
            # Embed with metrics collection if debug mode
            if collect_debug:
                result = embed_texts([q], collect_metrics=True, model=embedding_model, dimension=embedding_dim)
                embeddings, emb_metrics = result
                q_emb = embeddings[0] if embeddings else []
                # Store only the first embedding metrics (they're all the same model/config)
                if idx == 0:
                    embedding_metrics = emb_metrics
            else:
                q_emb = embed_text(q, model=embedding_model, dimension=embedding_dim)

            # Query vector store with metrics collection if debug mode
            # Filter by embedding_model to ensure we only get results from the same model
            if collect_debug:
                result = store.query_by_embedding(q_emb, k=k, collect_metrics=True, embedding_model=actual_model)
                results, vs_metrics = result
                total_raw_results += vs_metrics.raw_results_count
                # Accumulate scores for overall statistics
                if vs_metrics.score_min is not None:
                    all_scores.extend([r.get("score", 0.0) for r in results])
                # Store metrics from first query
                if idx == 0:
                    vector_search_metrics = vs_metrics
            else:
                results = store.query_by_embedding(q_emb, k=k, embedding_model=actual_model)

            for r in results:
                text = (r.get("text") or "").strip()
                if not text:
                    continue

                # Deduplicate across all queries
                norm = " ".join(text.split())
                if norm in seen_texts:
                    continue

                seen_texts.add(norm)
                all_results.append(r)
    finally:
        store.close()

    # Sort by score (highest first) and filter
    all_results.sort(key=lambda r: r.get("score", 0.0), reverse=True)

    # Track filtering for debug
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
        vector_search_time_ms=vector_search_metrics.vector_search_time_ms if vector_search_metrics else 0.0,
        filtering_time_ms=filtering_time_ms,
        composition_time_ms=0.0,  # compose_context is called separately
        original_query=query,
        query_variants=queries,
        num_queries=len(queries),
        embedding_model=embedding_metrics.embedding_model if embedding_metrics else "",
        embedding_dim=embedding_metrics.embedding_dim if embedding_metrics else 0,
        num_texts_embedded=embedding_metrics.num_texts_embedded if embedding_metrics else 0,
        batch_size=embedding_metrics.batch_size if embedding_metrics else 0,
        raw_results_count=total_raw_results if len(queries) > 1 else (vector_search_metrics.raw_results_count if vector_search_metrics else 0),
        score_min=overall_score_min,
        score_max=overall_score_max,
        score_mean=overall_score_mean,
        after_min_score_filter=after_min_score,
        after_dedupe_filter=after_dedupe,
        after_max_chunks_filter=after_max_chunks,
        total_chars_before_truncation=0,  # Not available at this level
        total_chars_after_truncation=0,  # Not available at this level
        chunks_included=len(filtered),
    )

    return filtered, debug_info


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

    parts: List[str] = []
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
            if remaining <= 0:
                break
            if len(block) > remaining:
                block = block[:remaining].rstrip()

        parts.append(block)
        used += len(block) + 2

        if max_chars > 0 and used >= max_chars:
            break

    return "\n\n".join(parts)