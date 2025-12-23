from __future__ import annotations

import io
import logging
import os
import uuid
from contextlib import redirect_stderr, redirect_stdout
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
import urllib.request
import urllib.error

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.status import HTTP_401_UNAUTHORIZED
from mygpt.api_models import (
    InfoResponse,
    SessionsListResponse,
    TitleRequest,
    TagsRequest,
    ChatRequest,
    ChatResponse,
    ToolTextResponse,
    ToolLsRequest,
    ToolCatRequest,
    ToolGrepRequest,
    RagIngestRequest,
    RagIngestResponse,
    RagQueryRequest,
    RagQueryResult,
    RagQueryResponse,
)

from mygpt.config import (
    get_default_model,
    get_ollama_base_url,
    get_sessions_dir,
    load_config,
)
from mygpt.chat import chat as run_chat
from mygpt import sessions
from mygpt import tools_fs

from mygpt.rag.rag import ingest_document, retrieve_context


log = logging.getLogger("mygpt.api")

# ----------------------------
# Startup logging configuration
# ----------------------------

_cfg_for_logging = load_config(None)
_log_level_name = _cfg_for_logging.get("logging", "level", fallback="INFO").upper()
_log_level = getattr(logging, _log_level_name, logging.INFO)

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

log.setLevel(_log_level)
log.info("Logging initialized (level=%s)", _log_level_name)

# ----------------------------
# Startup diagnostics using lifespan
# ----------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup diagnostics
    cfg = load_config(None)

    # Ensure sessions directory exists
    sessions_dir = get_sessions_dir(cfg)
    try:
        sessions_dir.mkdir(parents=True, exist_ok=True)
        log.info("Sessions directory ready: %s", sessions_dir)
    except Exception as e:
        log.error("Failed to prepare sessions directory %s: %s", sessions_dir, e)

    # Warn-only Ollama reachability check
    base_url = get_ollama_base_url(cfg).rstrip("/")
    health_url = f"{base_url}/api/tags"
    try:
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=2):
            log.info("Ollama reachable at %s", base_url)
    except urllib.error.URLError as e:
        log.warning(
            "Ollama not reachable at startup (%s). API will still start; chat requests may fail until Ollama is available.",
            e,
        )

    yield


# Versioned API router
app = FastAPI(title="myGPT", version="1.0.0", lifespan=lifespan)
api = APIRouter(prefix="/api/v1")


# CORS: default to local-only origins (configurable via MYGPT_CORS_ORIGINS)
# Example: export MYGPT_CORS_ORIGINS="http://127.0.0.1:3000,http://localhost:3000"
_origins_env = os.environ.get("MYGPT_CORS_ORIGINS", "").strip()
if _origins_env:
    allow_origins = [o.strip() for o in _origins_env.split(",") if o.strip()]
else:
    allow_origins = [
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_BODY_BYTES = int(os.environ.get("MYGPT_MAX_BODY_BYTES", "1048576"))  # 1 MiB default


@app.middleware("http")
async def add_request_id_and_limits(request: Request, call_next):
    # Request size guard based on Content-Length when available
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > MAX_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "payload_too_large",
                            "message": "Request body too large",
                        }
                    },
                )
        except Exception:
            # Ignore malformed content-length
            pass

    req_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = req_id

    response = await call_next(request)
    response.headers["X-Request-Id"] = req_id
    return response


@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    path = request.url.path

    # Allow unauthenticated access to health and docs
    if path == "/health" or path.startswith("/docs") or path.startswith("/openapi") or path.startswith("/redoc"):
        return await call_next(request)

    # Only protect versioned API
    if not path.startswith("/api/v1"):
        return await call_next(request)

    auth = _auth_cfg()
    if not auth.get("enabled"):
        return await call_next(request)

    header = auth.get("header", "X-API-Key")
    expected = auth.get("api_key")
    provided = request.headers.get(header)

    if not expected or provided != expected:
        req_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=HTTP_401_UNAUTHORIZED,
            content={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or missing API key",
                    "request_id": req_id,
                }
            },
        )

    return await call_next(request)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    req_id = getattr(request.state, "request_id", None)
    detail = exc.detail
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": "http_error",
                "message": detail if isinstance(detail, str) else "Request failed",
                "details": None if isinstance(detail, str) else detail,
                "request_id": req_id,
            }
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, "request_id", None)
    log.exception("Unhandled API error (request_id=%s)", req_id)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "Internal server error",
                "request_id": req_id,
            }
        },
    )


# ----------------------------
# Helpers
# ----------------------------

# Cached auth config (loaded once)
_AUTH_CFG: dict[str, Any] | None = None


def _auth_cfg() -> dict[str, Any]:
    global _AUTH_CFG
    if _AUTH_CFG is None:
        cfg = load_config(None)
        enabled = cfg.getboolean("auth", "enabled", fallback=False)
        api_key = cfg.get("auth", "api_key", fallback="").strip()
        header = cfg.get("auth", "header", fallback="X-API-Key").strip() or "X-API-Key"
        _AUTH_CFG = {
            "enabled": enabled,
            "api_key": api_key,
            "header": header,
        }
        if enabled:
            if not api_key:
                log.warning("Auth enabled but no api_key configured; all /api/v1 requests will be rejected")
            log.info("Auth enabled (header=%s)", header)
        else:
            log.info("Auth disabled")
    return _AUTH_CFG


def _cfg(cfg_path: Path | None = None):
    return load_config(cfg_path)


def _sessions_dir_from_str(s: str | None) -> Path | None:
    if not s:
        return None
    return Path(s).expanduser()


def _capture_stdout(fn, *args, **kwargs) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(*args, **kwargs)
    return int(rc), out.getvalue(), err.getvalue()




# ----------------------------
# Routes
# ----------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@api.get("/info", response_model=InfoResponse)
def info() -> InfoResponse:
    cfg = _cfg(None)
    base_url = get_ollama_base_url(cfg)
    model = get_default_model(cfg)
    return InfoResponse(
        ollama_base_url=base_url,
        default_model=model,
        sessions_dir=str(get_sessions_dir(cfg)),
    )


@api.get("/sessions", response_model=SessionsListResponse)
def sessions_list(sessions_dir: Optional[str] = None) -> SessionsListResponse:
    cfg = _cfg(None)
    effective_dir = _sessions_dir_from_str(sessions_dir) or get_sessions_dir(cfg)
    rows = sessions.list_sessions(effective_dir)
    out: list[dict[str, Any]] = []
    for r in rows:
        meta = r.get("meta") or {}
        out.append(
            {
                "name": r.get("name"),
                "messages": r.get("messages"),
                "modified": r.get("modified"),
                "pinned": bool(meta.get("pinned")),
                "tags": meta.get("tags") if isinstance(meta.get("tags"), list) else [],
                "title": meta.get("title") if isinstance(meta.get("title"), str) else "",
                "summary": meta.get("summary") if isinstance(meta.get("summary"), str) else "",
                "token_estimate": meta.get("token_estimate") if isinstance(meta.get("token_estimate"), int) else None,
                "model": meta.get("model") if isinstance(meta.get("model"), str) else "",
            }
        )
    return SessionsListResponse(sessions=out)


@api.get("/sessions/{name}")
def sessions_show(name: str, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    sd = _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None))
    sf = sessions.session_file_for(name, sd or sessions.default_sessions_dir())
    mf = sessions.meta_file_for(sf)
    msgs = sessions.load_session_messages(sf)
    meta = sessions.load_session_meta(mf)
    if not sf.exists():
        raise HTTPException(status_code=404, detail="No such session")
    return {"name": name, "messages": msgs, "meta": meta}


@api.delete("/sessions/{name}")
def sessions_delete(name: str, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    ok = sessions.delete_session(name, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)))
    if not ok:
        raise HTTPException(status_code=404, detail="No such session")
    return {"ok": True}


@api.post("/sessions/{name}/summarize")
def sessions_summarize(name: str, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    ok, msg = sessions.summarize_session(name, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@api.post("/sessions/{name}/pin")
def sessions_pin(name: str, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    ok, msg = sessions.set_pinned(name, True, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@api.post("/sessions/{name}/unpin")
def sessions_unpin(name: str, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    ok, msg = sessions.set_pinned(name, False, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@api.post("/sessions/{name}/title")
def sessions_title(name: str, req: TitleRequest, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    ok, msg = sessions.set_title(name, req.title, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@api.post("/sessions/{name}/tags/add")
def sessions_tags_add(name: str, req: TagsRequest, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    if not req.tags:
        raise HTTPException(status_code=400, detail="At least one tag is required")
    ok, msg = sessions.add_tags(name, req.tags, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@api.post("/sessions/{name}/tags/remove")
def sessions_tags_remove(name: str, req: TagsRequest, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    if not req.tags:
        raise HTTPException(status_code=400, detail="At least one tag is required")
    ok, msg = sessions.remove_tags(name, req.tags, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@api.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    try:
        result = run_chat(
            req.prompt,
            session=req.session,
            new=req.new,
            model=req.model,
            system=req.system,
            config_path=None,
            sessions_dir=req.sessions_dir,
        )
        return ChatResponse(session=result.session, model=result.model, reply=result.reply)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@api.post("/tools/ls", response_model=ToolTextResponse)
def tool_ls(req: ToolLsRequest) -> ToolTextResponse:
    rc, out, err = _capture_stdout(tools_fs.ls, Path(req.path))
    if rc != 0:
        raise HTTPException(status_code=400, detail=(err.strip() or out.strip() or "ls failed"))
    return ToolTextResponse(output=out)


@api.post("/tools/cat", response_model=ToolTextResponse)
def tool_cat(req: ToolCatRequest) -> ToolTextResponse:
    rc, out, err = _capture_stdout(tools_fs.cat, Path(req.path), head=req.head, tail=req.tail)
    if rc != 0:
        raise HTTPException(status_code=400, detail=(err.strip() or out.strip() or "cat failed"))
    return ToolTextResponse(output=out)


@api.post("/tools/grep", response_model=ToolTextResponse)
def tool_grep(req: ToolGrepRequest) -> ToolTextResponse:
    rc, out, err = _capture_stdout(tools_fs.grep, req.pattern, Path(req.path), max_matches=req.max)
    if rc != 0:
        raise HTTPException(status_code=400, detail=(err.strip() or out.strip() or "grep failed"))
    return ToolTextResponse(output=out)


@api.post("/rag/ingest", response_model=RagIngestResponse)
def rag_ingest(req: RagIngestRequest) -> RagIngestResponse:
    try:
        n = ingest_document(
            doc_id=req.doc_id,
            text=req.text,
            metadata=req.metadata,
            ensure_schema=req.ensure_schema,
        )
        return RagIngestResponse(doc_id=req.doc_id, chunks_ingested=n)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api.post("/rag/query", response_model=RagQueryResponse)
def rag_query(req: RagQueryRequest) -> RagQueryResponse:
    try:
        results = retrieve_context(req.query, top_k=req.top_k)
        out = [
            RagQueryResult(
                doc_id=str(r.get("doc_id", "")),
                chunk_id=int(r.get("chunk_id", 0)),
                text=str(r.get("text", "")),
                score=float(r.get("score", 0.0)),
            )
            for r in results
        ]
        return RagQueryResponse(results=out)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


app.include_router(api)