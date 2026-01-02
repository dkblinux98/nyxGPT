from __future__ import annotations

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


class TagsRequest(BaseModel):
    tags: list[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    session: str = "default"
    new: bool = False
    model: str | None = None
    system: str | None = None
    sessions_dir: str | None = None


class ChatResponse(BaseModel):
    session: str
    model: str
    reply: str


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
