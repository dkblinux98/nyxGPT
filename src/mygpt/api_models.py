from __future__ import annotations

from enum import Enum
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
    new_name: str = Field(..., min_length=1, max_length=200, description="New session name or title")
    sync_filename: bool = Field(True, description="Automatically sync filename with sanitized title")


class EditMessageRequest(BaseModel):
    """Request model for editing a message in a session."""
    content: str = Field(..., min_length=1, description="New content for the message")
    fork: bool = Field(True, description="Fork conversation after this message (truncate following messages)")


class RegenerateRequest(BaseModel):
    """Request model for regenerating a response.

    Can optionally include new prompt text or model override.
    """
    prompt: str | None = Field(None, description="Optional new prompt to replace user message before regenerating")
    model: str | None = Field(None, description="Optional model override for regeneration")
    rag_enabled: bool | None = Field(None, description="Optional RAG override for regeneration")


class TagsRequest(BaseModel):
    tags: list[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    session: str = "default"
    new: bool = False
    model: str | None = None
    system: str | None = None
    sessions_dir: str | None = None
    rag_enabled: bool | None = None  # Override session RAG setting


class RagChunkInfo(BaseModel):
    """Information about a single RAG chunk retrieved for context."""
    text: str
    score: float
    doc_id: str | None = None
    chunk_id: int | None = None


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


class RagIngestResponse(BaseModel):
    doc_id: str
    chunks_ingested: int


class RagQueryRequest(BaseModel):
    query: str = Field(..., description="Search query")
    top_k: int = Field(5, ge=1, le=50)


class RagQueryResult(BaseModel):
    doc_id: str
    chunk_id: int
    text: str
    score: float


class RagQueryResponse(BaseModel):
    results: list[RagQueryResult]


# ----------------------------
# Search API models
# ----------------------------


class MessageRole(str, Enum):
    """Valid message roles for filtering."""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class SearchRequest(BaseModel):
    """Request model for message search."""
    query: str = Field(..., min_length=1, description="Text to search for in messages")
    case_sensitive: bool = Field(False, description="Whether to perform case-sensitive search")
    role_filter: MessageRole | None = Field(None, description="Filter by message role (user, assistant, system)")
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
