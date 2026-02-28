from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict

from pydantic import BaseModel, Field

# ----------------------------
# Core API models
# ----------------------------


class SessionInfo(TypedDict):
    """Type-safe structure for session list items."""

    name: str
    file: str
    messages: int
    modified: str
    meta: dict[str, Any]


class InfoResponse(BaseModel):
    """Response model for /info endpoint."""

    ollama_base_url: str
    default_model: str
    sessions_dir: str
    release_version: str | None = None


class SessionsListResponse(BaseModel):
    """Response model for /sessions endpoint.

    Returns list of sessions with their metadata.
    """

    sessions: list[dict[str, Any]]


class TitleRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class RenameRequest(BaseModel):
    """Request model for renaming a session.

    Allows renaming session with automatic title update and filename sync.
    """

    new_name: str = Field(
        ..., min_length=1, max_length=200, description="New session name or title"
    )
    sync_filename: bool = Field(
        True, description="Automatically sync filename with sanitized title"
    )


class EditMessageRequest(BaseModel):
    """Request model for editing a message in a session."""

    content: str = Field(..., min_length=1, description="New content for the message")
    fork: bool = Field(
        True,
        description="Fork conversation after this message (truncate following messages)",
    )


class RegenerateRequest(BaseModel):
    """Request model for regenerating a response.

    Can optionally include new prompt text or model override.
    """

    prompt: str | None = Field(
        None,
        description="Optional new prompt to replace user message before regenerating",
    )
    model: str | None = Field(None, description="Optional model override for regeneration")
    rag_enabled: bool | None = Field(None, description="Optional RAG override for regeneration")


class TagsRequest(BaseModel):
    tags: list[str] = Field(default_factory=list)


class RagFilters(BaseModel):
    """Metadata filters for RAG document selection."""

    doc_ids: list[str] | None = Field(None, description="Filter by document IDs (OR logic)")
    filename: str | None = Field(
        None, description="Filter by filename (partial match, case-insensitive)"
    )
    tags: list[str] | None = Field(None, description="Filter by tags (must have ALL tags)")
    date_from: str | None = Field(None, description="Filter by ingestion date >= (ISO format)")
    date_to: str | None = Field(None, description="Filter by ingestion date <= (ISO format)")


class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    session: str = "default"
    new: bool = False
    model: str | None = None
    system: str | None = None
    sessions_dir: str | None = None
    rag_enabled: bool | None = None  # Override session RAG setting
    rag_filters: RagFilters | None = None  # Metadata filters for RAG queries


class RagChunkInfo(BaseModel):
    """Information about a single RAG chunk retrieved for context."""

    text: str
    score: float
    doc_id: str | None = None
    chunk_id: int | None = None
    similarity_score: float | None = None  # Original vector similarity (0-1) for UI display


class ChatResponse(BaseModel):
    session: str
    model: str
    reply: str
    rag_used: bool = False
    rag_chunks: list[RagChunkInfo] = Field(default_factory=list)


# ----------------------------
# Tools API models
# ----------------------------


class ToolTextResponse(BaseModel):
    output: str


class ToolLsRequest(BaseModel):
    path: str


class ToolCatRequest(BaseModel):
    path: str
    head: int | None = None
    tail: int | None = None


class ToolGrepRequest(BaseModel):
    pattern: str
    path: str
    max: int = 20


# ----------------------------
# RAG API models
# ----------------------------


class RagIngestRequest(BaseModel):
    doc_id: str = Field(..., description="Document identifier")
    text: str = Field(..., description="Raw document text")
    metadata: dict[str, Any] | None = None
    ensure_schema: bool = False
    collection: str = Field("default", description="Target collection name")


class RagIngestResponse(BaseModel):
    doc_id: str
    chunks_ingested: int
    status: str = Field(description="Ingestion status: 'ingested', 'updated', or 'skipped'")
    doc_hash: str | None = Field(None, description="SHA-256 hash of document content")
    previous_hash: str | None = Field(None, description="Previous hash if document was updated")


class RagDocumentInfo(BaseModel):
    doc_id: str
    doc_hash: str | None = Field(None, description="SHA-256 hash of document content")
    ingested_at: str | None = Field(None, description="Timestamp when document was first ingested")
    updated_at: str | None = Field(None, description="Timestamp when document was last updated")
    chunks: int = Field(description="Number of chunks in document")
    embedding_model: str | None = Field(None, description="Embedding model used for this document")


class RagIndexRepoRequest(BaseModel):
    repo_path: str = Field(..., description="Path to repository root")
    doc_id_prefix: str = Field("code", description="Prefix for document IDs")
    extensions: list[str] | None = Field(
        None, description="File extensions to include (e.g., ['.py', '.js'])"
    )
    extract_docs_only: bool = Field(
        False, description="Extract only comments/docstrings (exclude code)"
    )
    ensure_schema: bool = Field(False, description="Create schema if missing")
    collection: str = Field("default", description="Target collection name")


class RagIndexRepoResponse(BaseModel):
    total_files: int = Field(description="Number of files indexed")
    total_chunks: int = Field(description="Total chunks ingested")
    files: list[str] = Field(description="List of indexed file paths")
    doc_ids: list[str] = Field(description="List of document IDs created")


class RagQueryRequest(BaseModel):
    query: str = Field(..., description="Search query")
    top_k: int = Field(5, ge=1, le=50)
    debug_mode: bool = Field(False, description="Enable debug mode to return detailed metrics")
    # Metadata filters
    doc_ids: list[str] | None = Field(None, description="Filter by document IDs (OR logic)")
    filename: str | None = Field(
        None, description="Filter by filename (partial match, case-insensitive)"
    )
    tags: list[str] | None = Field(None, description="Filter by tags (must have ALL tags)")
    date_from: str | None = Field(None, description="Filter by ingestion date >= (ISO format)")
    date_to: str | None = Field(None, description="Filter by ingestion date <= (ISO format)")


class RagQueryResult(BaseModel):
    doc_id: str
    chunk_id: int
    text: str
    score: float
    similarity_score: float | None = None  # Original vector similarity (0-1) for UI display


class RagDebugInfo(BaseModel):
    """Debug information for RAG query troubleshooting."""

    # Timing metrics (milliseconds)
    total_time_ms: float
    query_expansion_time_ms: float | None = None
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
    score_min: float | None = None
    score_max: float | None = None
    score_mean: float | None = None

    # Filtering stats
    after_min_score_filter: int
    after_dedupe_filter: int
    after_max_chunks_filter: int

    # Context composition
    total_chars_before_truncation: int
    total_chars_after_truncation: int
    chunks_included: int


class RagQueryResponse(BaseModel):
    results: list[RagQueryResult]
    debug_info: RagDebugInfo | None = None


class RetrievalAccuracyMetrics(BaseModel):
    """Metrics for evaluating retrieval accuracy and quality."""

    results_returned: int
    query_success: bool
    unique_docs_retrieved: int
    total_chunks_retrieved: int
    score_distribution: dict[str, float]


class LatencyMetrics(BaseModel):
    """Enhanced latency tracking with percentile breakdowns."""

    total_time_ms: float
    stage_timings: dict[str, float]
    percentiles: dict[str, float] | None = None


class HitRateMetrics(BaseModel):
    """Metrics for hit rate analysis and query patterns."""

    query_success_rate: float
    total_queries: int
    successful_queries: int
    failed_queries: int
    avg_top_score: float | None
    score_above_threshold_rate: float


class RagEvaluationMetrics(BaseModel):
    """Comprehensive evaluation metrics for RAG quality."""

    retrieval_accuracy: RetrievalAccuracyMetrics
    latency: LatencyMetrics
    hit_rate: HitRateMetrics
    query_id: str
    timestamp: float


class RagMetricsQueryRequest(BaseModel):
    """Request model for querying RAG metrics with evaluation."""

    query: str = Field(..., min_length=1)
    top_k: int | None = None
    debug_mode: bool = True
    collect_metrics: bool = True


class RagMetricsQueryResponse(BaseModel):
    """Response model for RAG query with evaluation metrics."""

    results: list[RagQueryResult]
    debug_info: RagDebugInfo | None = None
    evaluation_metrics: RagEvaluationMetrics | None = None


# ----------------------------
# Collection Management API models
# ----------------------------


class CollectionInfo(BaseModel):
    """Information about a RAG collection."""

    name: str = Field(..., description="Collection name")
    doc_count: int = Field(..., description="Number of documents in collection")
    chunk_count: int = Field(..., description="Total number of chunks in collection")
    embedding_models: list[str] = Field(..., description="Embedding models used in this collection")


class CollectionsListResponse(BaseModel):
    """Response model for listing collections."""

    collections: list[CollectionInfo]


class CollectionDeleteResponse(BaseModel):
    """Response model for collection deletion."""

    collection: str = Field(..., description="Name of deleted collection")
    status: str = Field(..., description="Deletion status message")


class CreateCollectionRequest(BaseModel):
    """Request model for creating a new collection."""

    name: str = Field(..., description="Collection name (alphanumeric and underscores only)")
    embedding_dim: int = Field(..., description="Embedding dimension (e.g., 768, 1536)")
    embedding_model: str | None = Field(
        None, description="Embedding model name (optional, for documentation)"
    )


class CreateCollectionResponse(BaseModel):
    """Response model for collection creation."""

    collection: str = Field(..., description="Name of created collection")
    status: str = Field(..., description="Creation status message")
    embedding_dim: int = Field(..., description="Embedding dimension")


class ReindexCollectionRequest(BaseModel):
    """Request model for re-indexing a collection."""

    target_embedding_model: str = Field(..., description="Target embedding model for re-indexing")
    embedding_dim: int = Field(..., description="New embedding dimension")


class ReindexCollectionResponse(BaseModel):
    """Response model for collection re-indexing."""

    collection: str = Field(..., description="Name of collection being re-indexed")
    status: str = Field(..., description="Re-indexing status message")
    chunks_processed: int = Field(..., description="Number of chunks processed")
    chunks_total: int = Field(..., description="Total number of chunks")


class CollectionSettings(BaseModel):
    """Collection settings configuration."""

    embedding_model: str | None = Field(None, description="Preferred embedding model")
    chunk_size: int | None = Field(None, description="Default chunk size for documents")
    chunk_overlap: int | None = Field(None, description="Chunk overlap in characters")


class CollectionSettingsResponse(BaseModel):
    """Response model for collection settings."""

    collection: str = Field(..., description="Collection name")
    settings: CollectionSettings = Field(..., description="Collection settings")


# ----------------------------
# Search API models
# ----------------------------


class MessageRole(StrEnum):
    """Valid message roles for filtering."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class SearchRequest(BaseModel):
    """Request model for message search."""

    query: str = Field(..., min_length=1, description="Text to search for in messages")
    case_sensitive: bool = Field(False, description="Whether to perform case-sensitive search")
    role_filter: MessageRole | None = Field(
        None, description="Filter by message role (user, assistant, system)"
    )
    session_filter: str | None = Field(None, description="Filter to specific session name")
    limit: int = Field(50, ge=1, le=500, description="Maximum number of results to return")


class SearchResultItem(BaseModel):
    """Single search result item."""

    session_name: str = Field(..., description="Name of the session containing the match")
    session_title: str | None = Field(None, description="Title of the session if available")
    message_index: int = Field(..., description="Index of the message in the session (0-based)")
    role: str = Field(..., description="Role of the message author (user, assistant, system)")
    content: str = Field(..., description="Full content of the matching message")
    content_preview: str = Field(..., description="Preview snippet showing match in context")
    timestamp: str | None = Field(None, description="Timestamp of the message if available")
    matches: int = Field(..., description="Number of times the query appears in this message")


class SearchResponse(BaseModel):
    """Response model for message search."""

    query: str = Field(..., description="The search query that was executed")
    total_results: int = Field(..., description="Total number of results found")
    results: list[SearchResultItem] = Field(..., description="List of matching messages")


# ----------------------------
# Resource Monitoring API models
# ----------------------------


class AttachDocumentRequest(BaseModel):
    """Request model for attaching a document to a session."""

    doc_id: str = Field(..., min_length=1, description="Document ID to attach to the session")


class SessionDocumentsResponse(BaseModel):
    """Response model for session document attachment operations."""

    session: str = Field(..., description="Session name")
    attached_doc_ids: list[str] = Field(
        default_factory=list,
        description="List of document IDs force-included for this session's RAG context",
    )


class ResourceMetricsResponse(BaseModel):
    """Response model for resource usage metrics endpoint.

    Provides comprehensive resource monitoring including memory, CPU,
    request latency, and queue depth metrics.
    """

    memory: dict[str, float] = Field(
        ...,
        description="Memory metrics (rss_mb, vms_mb, percent, available_mb)",
    )
    cpu: dict[str, float] = Field(
        ...,
        description="CPU metrics (process_percent, system_percent)",
    )
    latency: dict[str, float] = Field(
        ...,
        description="Request latency metrics (avg_ms, p50_ms, p95_ms, p99_ms)",
    )
    queue: dict[str, int] = Field(
        ...,
        description="Queue metrics (depth, total_requests)",
    )
