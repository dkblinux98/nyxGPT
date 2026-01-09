from __future__ import annotations

import io
import inspect
import os
import secrets
import uuid
from contextlib import redirect_stderr, redirect_stdout
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
import urllib.request
import urllib.error
from configparser import ConfigParser

from fastapi import APIRouter, FastAPI, HTTPException, Request, Body, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.status import HTTP_401_UNAUTHORIZED
from mygpt.api_models import (
    InfoResponse,
    SessionsListResponse,
    TitleRequest,
    RenameRequest,
    TagsRequest,
    ChatRequest,
    ChatResponse,
    RagChunkInfo,
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
    get_rate_limit_enabled,
    get_rate_limit_config,
    load_config,
)
from mygpt.chat import chat as run_chat, chat_stream
from mygpt import sessions
from mygpt import tools_fs
from mygpt import models

from mygpt.rag.rag import ingest_document, retrieve_context
from mygpt.logging import configure_logging, request_id_var
from mygpt.rate_limiter import RateLimiter



import logging
log = logging.getLogger("mygpt.api")

# Global rate limiter instance (initialized at startup if enabled)
_rate_limiter: RateLimiter | None = None


def log_with_context(level, message, request_id=None, **extra):
    """Helper for structured logging with consistent context."""
    extra_fields = {"request_id": request_id, **extra} if request_id else extra
    log.log(level, message, extra=extra_fields)


# ----------------------------
# Startup diagnostics using lifespan
# ----------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _rate_limiter

    # Initialize centralized logging once for the API process
    cfg = load_config(None)
    try:
        configure_logging(cfg, console=False)
        log.info("Centralized logging initialized", extra={"component": "startup"})
    except Exception as e:
        # Logging should not prevent API startup
        print(f"Logging initialization failed: {e}")

    # Ensure sessions directory exists
    sessions_dir = get_sessions_dir(cfg)
    try:
        sessions_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "Sessions directory ready",
            extra={"component": "startup", "sessions_dir": str(sessions_dir)}
        )
    except Exception as e:
        log.error("Failed to prepare sessions directory %s: %s", sessions_dir, e)

    # Warn-only Ollama reachability check
    base_url = get_ollama_base_url(cfg).rstrip("/")
    health_url = f"{base_url}/api/tags"
    try:
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=2):
            log.info(
                "Ollama health check passed",
                extra={"component": "startup", "ollama_url": base_url}
            )
    except urllib.error.URLError as e:
        log.warning(
            "Ollama health check failed",
            extra={
                "component": "startup",
                "ollama_url": base_url,
                "error": str(e),
                "note": "API will still start; chat requests may fail until Ollama is available"
            }
        )

    # Initialize rate limiter if enabled
    if get_rate_limit_enabled(cfg):
        rate_cfg = get_rate_limit_config(cfg)
        _rate_limiter = RateLimiter(
            requests_per_second=rate_cfg["requests_per_second"],
            burst_size=rate_cfg["burst_size"]
        )
        log.info(
            "Rate limiting enabled",
            extra={
                "component": "startup",
                "requests_per_second": rate_cfg["requests_per_second"],
                "burst_size": rate_cfg["burst_size"],
            }
        )
    else:
        log.info("Rate limiting disabled", extra={"component": "startup"})

    yield


# Versioned API router
app = FastAPI(title="myGPT", version="1.0.0.md", lifespan=lifespan)
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


# Security headers middleware
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add security headers to all responses.

    Headers added:
    - Content-Security-Policy: Restrict resource loading
    - X-Content-Type-Options: Prevent MIME sniffing
    - X-Frame-Options: Prevent clickjacking
    - Strict-Transport-Security: Force HTTPS (only for HTTPS requests)
    """
    response = await call_next(request)

    # Content Security Policy: Restrict to same origin and specific trusted sources
    # This prevents XSS by limiting where resources can be loaded from
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "  # Allow inline scripts for development
        "style-src 'self' 'unsafe-inline'; "   # Allow inline styles
        "img-src 'self' data:; "                # Allow data URIs for images
        "connect-src 'self'; "                  # API calls to same origin only
        "frame-ancestors 'none'; "              # Prevent embedding (redundant with X-Frame-Options)
        "form-action 'self'; "                  # Forms can only submit to same origin
        "base-uri 'self'"                       # Prevent base tag injection
    )

    # Prevent MIME type sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"

    # Prevent clickjacking by disallowing framing
    response.headers["X-Frame-Options"] = "DENY"

    # Strict-Transport-Security (HSTS) - only for HTTPS
    # Force browser to use HTTPS for 1 year
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )

    return response


MAX_BODY_BYTES = int(os.environ.get("MYGPT_MAX_BODY_BYTES", "1048576"))  # 1 MiB default


# Middleware to load config and hot-apply logging on every request
@app.middleware("http")
async def load_cfg_and_refresh_logging(request: Request, call_next):
    """Load config for this request and hot-apply logging level.

    We want edits to ~/.myGPT/config.ini (model, rag enabled, log level, auth, etc.)
    to take effect without restarting the API process.

    The loaded config is stored on request.state.cfg for reuse by downstream
    middleware/handlers.
    """

    cfg = load_config(None)
    request.state.cfg = cfg

    # Hot-apply logging config (especially level) on every request.
    # configure_logging() is expected to be idempotent and cheap.
    try:
        configure_logging(cfg, console=False)
    except Exception:
        # Never block request handling on logging reconfiguration.
        pass

    return await call_next(request)


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

    # Accept client-provided request ID or generate new one
    req_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = req_id

    # Set request ID in context variable for automatic logging
    request_id_var.set(req_id)

    response = await call_next(request)
    response.headers["X-Request-Id"] = req_id
    return response


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Apply rate limiting to API endpoints.

    Uses token bucket algorithm to limit requests per IP address.
    Returns 429 Too Many Requests when limit exceeded.
    """
    # Skip if rate limiting not enabled
    if _rate_limiter is None:
        return await call_next(request)

    # Get client ID (IP address)
    client_id = _rate_limiter.get_client_ip(request)

    # Check if allowed
    allowed, headers = _rate_limiter.is_allowed(client_id)

    if not allowed:
        # Log rate limit violation
        req_id = getattr(request.state, "request_id", None)
        log.warning(
            "Rate limit exceeded for %s on %s (request_id=%s)",
            client_id,
            request.url.path,
            req_id
        )

        # Return 429 error with rate limit headers
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "code": "rate_limit_exceeded",
                    "message": "Too many requests. Please try again later.",
                    "request_id": req_id
                }
            },
            headers=headers
        )

    # Add rate limit headers to response
    response = await call_next(request)
    for key, value in headers.items():
        response.headers[key] = value

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

    cfg = getattr(request.state, "cfg", None)
    auth = _auth_cfg(cfg)
    req_id = getattr(request.state, "request_id", None)
    log.debug(
        "auth check (request_id=%s) enabled=%s",
        req_id,
        bool(auth.get("enabled")),
    )
    if not auth.get("enabled"):
        return await call_next(request)

    header = auth.get("header", "X-API-Key")
    expected = auth.get("api_key")
    provided = request.headers.get(header)

    # Use constant-time comparison to prevent timing attacks
    # This prevents attackers from determining the correct API key
    # by measuring response times
    auth_valid = False
    if expected and provided:
        auth_valid = secrets.compare_digest(expected, provided)

    if not auth_valid:
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

# Request-scoped config helper
def _req_cfg(request: Request) -> ConfigParser:
    cfg = getattr(request.state, "cfg", None)
    if cfg is None:
        cfg = load_config(None)
        request.state.cfg = cfg
    return cfg

# Auth config is read on each request so ~/.myGPT/config.ini edits
# take effect without restarting the API.
def _auth_cfg(cfg: ConfigParser | None = None) -> dict[str, Any]:
    cfg = cfg or load_config(None)
    enabled = cfg.getboolean("auth", "enabled", fallback=False)
    api_key = cfg.get("auth", "api_key", fallback="").strip()
    header = cfg.get("auth", "header", fallback="X-API-Key").strip() or "X-API-Key"
    return {
        "enabled": enabled,
        "api_key": api_key,
        "header": header,
    }


# --- Config file helpers and hot-update endpoints ---

def _config_file_path() -> Path:
    # Canonical per-user config location
    return Path.home() / ".myGPT" / "config.ini"


def _apply_hot_config_updates(updates: dict[str, Any]) -> dict[str, Any]:
    """Apply a small set of hot config updates to ~/.myGPT/config.ini.

    Supported updates:
    - default_model (str) -> [ollama] default_model
    - rag_enabled (bool)  -> [rag] enabled
    - log_level (str)     -> [logging] level

    After writing, reload config and re-configure logging so log level changes apply immediately.
    Model and RAG are read per-request by chat endpoints, so they apply immediately as well.
    """

    cfg_path = _config_file_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    parser = ConfigParser()
    if cfg_path.exists():
        parser.read(cfg_path)

    def ensure_section(name: str) -> None:
        if not parser.has_section(name):
            parser.add_section(name)

    out: dict[str, Any] = {}

    if "default_model" in updates and isinstance(updates.get("default_model"), str):
        ensure_section("ollama")
        parser.set("ollama", "default_model", updates["default_model"].strip())
        out["default_model"] = updates["default_model"].strip()

    if "rag_enabled" in updates:
        ensure_section("rag")
        val = bool(updates["rag_enabled"])
        parser.set("rag", "enabled", "true" if val else "false")
        out["rag_enabled"] = val

    if "log_level" in updates and isinstance(updates.get("log_level"), str):
        ensure_section("logging")
        lvl = updates["log_level"].strip().upper()
        parser.set("logging", "level", lvl)
        out["log_level"] = lvl

    # Persist changes
    with cfg_path.open("w", encoding="utf-8") as f:
        parser.write(f)

    # Hot-apply logging changes immediately
    try:
        cfg = load_config(None)
        configure_logging(cfg, console=False)
    except Exception:
        # Do not fail the request if logging reconfig fails
        pass

    return out


# Ollama model management helpers
def _ollama_url(cfg: ConfigParser, path: str) -> str:
    base = get_ollama_base_url(cfg).rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _ollama_get_json(url: str, timeout_s: float = 10.0) -> Any:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read()
    import json
    return json.loads(data.decode("utf-8"))


def _ollama_post_json(url: str, payload: dict[str, Any], timeout_s: float = 60.0) -> Any:
    import json
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8"))


def _cfg(cfg_path: Path | None = None):
    return load_config(cfg_path)


def _chat_runtime_defaults(cfg: ConfigParser | None = None) -> dict[str, Any]:
    """Read chat-related defaults from config.ini.

    If a request-scoped config is available, reuse it so a single request does
    not re-read config.ini multiple times.

    Config edits (model, rag enabled, etc.) should take effect without restart.
    """

    cfg = cfg or load_config(None)
    rag_enabled = cfg.getboolean("rag", "enable_chat_context", fallback=False)
    return {
        "cfg": cfg,
        "default_model": get_default_model(cfg),
        "rag_enabled": rag_enabled,
    }


def _maybe_kw(fn, name: str) -> bool:
    """Return True if function `fn` accepts keyword argument `name`."""

    try:
        return name in inspect.signature(fn).parameters
    except Exception:
        return False


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
def info(request: Request) -> InfoResponse:
    cfg = _req_cfg(request)
    return InfoResponse(
        ollama_base_url=get_ollama_base_url(cfg),
        default_model=get_default_model(cfg),
        sessions_dir=str(get_sessions_dir(cfg)),
    )


# --- Config get/set endpoints ---

@api.get("/config")
def config_get(request: Request) -> dict[str, Any]:
    cfg = _req_cfg(request)
    return {
        "ollama_base_url": get_ollama_base_url(cfg),
        "default_model": get_default_model(cfg),
        "rag_enabled": cfg.getboolean("rag", "enabled", fallback=False),
        "log_level": cfg.get("logging", "level", fallback="INFO").strip().upper(),
    }


@api.post("/config")
def config_update(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    # Only apply known keys; ignore the rest.
    updates: dict[str, Any] = {}
    if "default_model" in payload:
        updates["default_model"] = payload.get("default_model")
    if "rag_enabled" in payload:
        updates["rag_enabled"] = payload.get("rag_enabled")
    if "log_level" in payload:
        updates["log_level"] = payload.get("log_level")

    changed = _apply_hot_config_updates(updates)

    # After applying, reload config and update request.state.cfg
    request.state.cfg = load_config(None)
    cfg = _req_cfg(request)
    return {
        "updated": changed,
        "effective": {
            "ollama_base_url": get_ollama_base_url(cfg),
            "default_model": get_default_model(cfg),
            "rag_enabled": cfg.getboolean("rag", "enabled", fallback=False),
            "log_level": cfg.get("logging", "level", fallback="INFO").strip().upper(),
        },
    }


# PATCH endpoint for config updates
@api.patch("/config")
def config_patch(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return config_update(request, payload)


# --- Model management endpoints ---
@api.get("/models")
def models_list(request: Request) -> dict[str, Any]:
    cfg = _req_cfg(request)
    try:
        data = _ollama_get_json(_ollama_url(cfg, "/api/tags"), timeout_s=10.0)
        models = data.get("models", []) if isinstance(data, dict) else []
        # Normalize to a list of model names
        names: list[str] = []
        for m in models:
            if isinstance(m, dict) and isinstance(m.get("name"), str):
                names.append(m["name"])
        return {"models": names}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to list models from Ollama: {e}")


@api.post("/models/pull")
def models_pull(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    cfg = _req_cfg(request)
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(status_code=400, detail="Missing 'model'")
    model = model.strip()
    try:
        # Non-streaming pull; Ollama may take a while.
        data = _ollama_post_json(_ollama_url(cfg, "/api/pull"), {"name": model, "stream": False}, timeout_s=600.0)
        return {"ok": True, "model": model, "result": data}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to pull model via Ollama: {e}")


@api.delete("/models/{model_name}")
def models_delete(request: Request, model_name: str) -> dict[str, Any]:
    """Delete a model from Ollama."""
    cfg = _req_cfg(request)
    try:
        models.delete_model(model_name, base_url=get_ollama_base_url(cfg))
        return {"ok": True, "model": model_name}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to delete model via Ollama: {e}")


@api.get("/models/{model_name}/info")
def models_info(request: Request, model_name: str) -> dict[str, Any]:
    """Get detailed information about a model."""
    cfg = _req_cfg(request)
    try:
        info = models.show_model(model_name, base_url=get_ollama_base_url(cfg))
        return {"ok": True, "model": model_name, "info": info}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to get model info via Ollama: {e}")


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

# Lightweight session initialization endpoint (does NOT invoke the model)

@api.post("/sessions/init")
def sessions_init(req: dict[str, Any] = Body(...)) -> dict[str, Any]:
    name = req.get("name")
    if not name or not isinstance(name, str):
        raise HTTPException(status_code=400, detail="Session name is required")

    cfg = _cfg(None)
    sd = get_sessions_dir(cfg)

    # Ensure sessions directory exists
    sd.mkdir(parents=True, exist_ok=True)

    # Idempotent behavior: if the session already exists, succeed
    try:
        sf = sessions.session_file_for(name, sd)
    except ValueError as e:
        # Validation errors should return 422 (Unprocessable Entity)
        log.warning("Invalid session name for init: %s", e)
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        # Other errors are internal server errors
        log.error("Failed to get session file path: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error")

    if sf.exists():
        return {"ok": True, "name": name, "existed": True}

    system = req.get("system")
    if not isinstance(system, str) or not system:
        system = "You are a helpful assistant."

    model = req.get("model")
    if not isinstance(model, str) or not model:
        model = get_default_model(cfg)

    try:
        _sf, _mf, _msgs, _meta = sessions.init_session(
            name,
            sd,
            new_session=True,
            model=model,
            system=system,
        )
    except Exception as e:
        log.exception("sessions.init_session failed")
        raise HTTPException(status_code=400, detail=str(e))

    return {"ok": True, "name": name, "existed": False}


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


@api.post("/sessions/{name}/rename")
def sessions_rename(name: str, req: RenameRequest, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    """Rename a session with optional title update and filename sync.

    This endpoint supports two modes:
    1. Direct rename: Provide a valid session name to rename files directly
    2. Title-based rename: Provide a title, which will be sanitized for use as filename

    If sync_filename=True (default), the session title will be updated and the
    filename will be synced to match the sanitized title.
    """
    _sessions_dir = _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None))

    # Check if current session exists
    sf = sessions.session_file_for(name, _sessions_dir)
    if not sf.exists():
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")

    if req.sync_filename:
        # Mode 1: Update title and sync filename
        # Set the title first
        ok, msg = sessions.set_title(name, req.new_name, _sessions_dir)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)

        # Then sync filename based on title
        success, status, new_name = sessions.sync_filename_with_title(name, _sessions_dir, force=True)
        if not success:
            raise HTTPException(status_code=500, detail=f"Title updated but filename sync failed: {status}")

        return {"ok": True, "old_name": name, "new_name": new_name, "message": "Session renamed and filename synced"}
    else:
        # Mode 2: Direct rename (validate new name first)
        try:
            validated_name = sessions.validate_session_name(req.new_name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # Use existing rename function
        ok, msg = sessions.rename_session(name, validated_name, _sessions_dir)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)

        return {"ok": True, "old_name": name, "new_name": validated_name, "message": "Session renamed"}


@api.post("/sessions/{name}/sync-filename")
def sessions_sync_filename(name: str, sessions_dir: Optional[str] = None) -> dict[str, Any]:
    """Force filename sync for a session based on its current title.

    This endpoint is useful when:
    - A session was created before auto-sync was enabled
    - Manual title changes were made without filename sync
    - You want to clean up session filenames to match their titles
    """
    _sessions_dir = _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None))

    # Check if session exists
    sf = sessions.session_file_for(name, _sessions_dir)
    if not sf.exists():
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")

    # Force filename sync
    success, status, new_name = sessions.sync_filename_with_title(name, _sessions_dir, force=True)

    if not success:
        raise HTTPException(status_code=500, detail=f"Filename sync failed: {status}")

    if status == "no_title":
        return {"ok": True, "message": "No title set, filename unchanged", "name": name}
    elif status == "no_change":
        return {"ok": True, "message": "Filename already matches title", "name": name}
    elif status == "renamed":
        return {"ok": True, "old_name": name, "new_name": new_name, "message": "Filename synced with title"}
    else:
        return {"ok": True, "message": status, "name": new_name}


@api.get("/sessions/{name}/export")
def sessions_export(name: str, format: str = "markdown", sessions_dir: Optional[str] = None):
    """Export session to markdown, JSON, or HTML format."""
    format_lower = format.lower()
    if format_lower not in ("markdown", "json", "html"):
        raise HTTPException(status_code=400, detail="Invalid format. Must be one of: markdown, json, html")

    sd = _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None))

    if format_lower == "markdown":
        ok, content = sessions.export_session_markdown(name, sd)
        media_type = "text/markdown; charset=utf-8"
        extension = "md"
    elif format_lower == "json":
        ok, content = sessions.export_session_json(name, sd)
        media_type = "application/json; charset=utf-8"
        extension = "json"
    else:
        ok, content = sessions.export_session_html(name, sd)
        media_type = "text/html; charset=utf-8"
        extension = "html"

    if not ok:
        raise HTTPException(status_code=404, detail=content)

    from fastapi.responses import Response
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{name}.{extension}"'})


@api.post("/chat", response_model=ChatResponse)
def chat(request: Request, req: ChatRequest) -> ChatResponse:
    req_id = getattr(request.state, "request_id", None)

    try:
        d = _chat_runtime_defaults(_req_cfg(request))
        chosen_model = req.model or d["default_model"]

        log.info(
            "Chat request received",
            extra={
                "request_id": req_id,
                "session": req.session,
                "model": chosen_model,
                "new_session": req.new,
                "prompt_length": len(req.prompt),
                "rag_enabled": d.get("rag_enabled", False),
            }
        )

        kwargs: dict[str, Any] = {
            "session": req.session,
            "new": req.new,
            "model": chosen_model,
            "system": req.system,
            "config_path": None,
            "sessions_dir": req.sessions_dir,
        }

        # Optional runtime override: only pass if chat implementation supports it.
        if _maybe_kw(run_chat, "rag_enabled"):
            rag_val = getattr(req, "rag_enabled", None)
            kwargs["rag_enabled"] = d["rag_enabled"] if rag_val is None else bool(rag_val)

        result = run_chat(req.prompt, **kwargs)

        log.info(
            "Chat request completed",
            extra={
                "request_id": req_id,
                "session": result.session,
                "model": result.model,
                "reply_length": len(result.reply),
            }
        )

        # Convert RAG context to RagChunkInfo objects
        rag_chunks = []
        if result.rag_context:
            for chunk_data in result.rag_context:
                rag_chunks.append(RagChunkInfo(
                    text=chunk_data.get("text", ""),
                    score=chunk_data.get("score", 0.0),
                    doc_id=chunk_data.get("doc_id"),
                    chunk_id=chunk_data.get("chunk_id"),
                ))

        return ChatResponse(
            session=result.session,
            model=result.model,
            reply=result.reply,
            rag_used=result.rag_used,
            rag_chunks=rag_chunks,
        )
    except ValueError as e:
        # Validation errors (e.g., invalid session name)
        log.warning("Chat validation error", extra={"request_id": req_id, "error": str(e)})
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        log.error(
            "Chat request failed",
            extra={"request_id": req_id, "error": str(e), "error_type": type(e).__name__},
            exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")


def _create_streaming_response(request: Request, req: ChatRequest) -> StreamingResponse:
    """Create a streaming chat response with request ID context propagation.

    This helper consolidates the streaming logic used by both versioned and legacy endpoints.
    It handles request ID capture and context setting for proper log traceability.

    Args:
        request: FastAPI Request object containing state and configuration
        req: Chat request parameters

    Returns:
        StreamingResponse configured for text/plain streaming

    Raises:
        HTTPException: 422 for validation errors, 500 for server errors
    """
    try:
        # Validate session name early, before creating generator
        # This ensures validation errors are caught and return 422 instead of failing during streaming
        from mygpt.sessions import validate_session_name
        try:
            validate_session_name(req.session)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

        # Capture request ID before entering generator (context may not propagate)
        req_id = request.state.request_id

        def _stream_with_keepalive():
            # Explicitly set request ID in context for streaming generator
            try:
                request_id_var.set(req_id)
            except Exception as e:
                log.warning(f"Failed to set request ID in streaming context: {e}")
            # Continue regardless - streaming should work even if request ID fails

            # Send an immediate keepalive to prevent client read timeouts
            yield "\n"
            d = _chat_runtime_defaults(_req_cfg(request))
            chosen_model = req.model or d["default_model"]

            kwargs: dict[str, Any] = {
                "session": req.session,
                "new": req.new,
                "model": chosen_model,
                "system": req.system,
                "config_path": None,
                "sessions_dir": req.sessions_dir,
            }

            if _maybe_kw(chat_stream, "rag_enabled"):
                rag_val = getattr(req, "rag_enabled", None)
                kwargs["rag_enabled"] = d["rag_enabled"] if rag_val is None else bool(rag_val)

            for chunk in chat_stream(req.prompt, **kwargs):
                yield chunk

        return StreamingResponse(
            _stream_with_keepalive(),
            media_type="text/plain; charset=utf-8",
        )
    except HTTPException:
        # Re-raise HTTP exceptions (e.g., 422 from validation above)
        raise
    except Exception as e:
        log.error(f"Streaming setup failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# Streaming chat endpoint
@api.post("/chat/stream")
def chat_stream_api(request: Request, req: ChatRequest):
    """Streaming chat endpoint (API v1).

    Delegates to shared streaming response helper.
    """
    return _create_streaming_response(request, req)


# Non-versioned alias for streaming chat endpoint
@app.post("/api/chat/stream")
def chat_stream_api_legacy(request: Request, req: ChatRequest):
    """Streaming chat endpoint (legacy, non-versioned).

    Delegates to shared streaming response helper.
    """
    return _create_streaming_response(request, req)


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
def rag_ingest(request: Request, req: RagIngestRequest) -> RagIngestResponse:
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
def rag_query(request: Request, req: RagQueryRequest) -> RagQueryResponse:
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


@api.get("/sessions/{name}/metadata")
def get_session_metadata(name: str) -> dict[str, Any]:
    """Get metadata for a specific session."""
    cfg = load_config(None)
    sessions_dir = get_sessions_dir(cfg)
    sf = sessions.session_file_for(name, sessions_dir)
    mf = sessions.meta_file_for(sf)

    # Create session files if they don't exist
    if not sf.exists():
        sessions.save_session_messages(sf, [])

    meta = sessions.load_session_meta(mf)
    meta = sessions.ensure_meta_defaults(meta)
    sessions.save_session_meta(mf, meta)

    return meta


@api.post("/sessions/{name}/rag/enable")
def enable_session_rag(name: str) -> dict[str, Any]:
    """Enable RAG for a specific session.

    Note: This endpoint uses non-atomic read-modify-write operations.
    For single-user applications, this is acceptable. For multi-user
    deployments, consider file locking or database transactions.
    """
    cfg = load_config(None)
    sessions_dir = get_sessions_dir(cfg)
    sf = sessions.session_file_for(name, sessions_dir)
    mf = sessions.meta_file_for(sf)

    # Create session files if they don't exist
    if not sf.exists():
        sessions.save_session_messages(sf, [])

    meta = sessions.load_session_meta(mf)
    meta = sessions.ensure_meta_defaults(meta)
    meta["rag_enabled"] = True
    sessions.save_session_meta(mf, meta)

    return {"session": name, "rag_enabled": True}


@api.post("/sessions/{name}/rag/disable")
def disable_session_rag(name: str) -> dict[str, Any]:
    """Disable RAG for a specific session.

    Note: This endpoint uses non-atomic read-modify-write operations.
    For single-user applications, this is acceptable. For multi-user
    deployments, consider file locking or database transactions.
    """
    cfg = load_config(None)
    sessions_dir = get_sessions_dir(cfg)
    sf = sessions.session_file_for(name, sessions_dir)
    mf = sessions.meta_file_for(sf)

    # Create session files if they don't exist
    if not sf.exists():
        sessions.save_session_messages(sf, [])

    meta = sessions.load_session_meta(mf)
    meta = sessions.ensure_meta_defaults(meta)
    meta["rag_enabled"] = False
    sessions.save_session_meta(mf, meta)

    return {"session": name, "rag_enabled": False}


@api.post("/rag/upload", response_model=RagIngestResponse)
async def rag_upload_file(
    file: UploadFile = File(...),
    doc_id: str | None = None,
) -> RagIngestResponse:
    """Upload and ingest a document for RAG with proper markdown parsing."""
    # Validate file type
    allowed_types = {".txt", ".md", ".json", ".pdf"}
    file_ext = Path(file.filename or "").suffix.lower()
    if file_ext not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"File type {file_ext} not supported. Allowed: {', '.join(allowed_types)}"
        )

    # Read file content
    content = await file.read()

    # Validate file size (prevent memory exhaustion)
    MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(content)} bytes). Maximum size: {MAX_UPLOAD_SIZE} bytes (10MB)"
        )

    # Parse based on file type
    if file_ext == ".pdf":
        # Handle PDF (if pypdf available)
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            text = "\n\n".join(page.extract_text() for page in reader.pages)
        except ImportError:
            raise HTTPException(status_code=400, detail="PDF support not available. Install pypdf: pip install pypdf")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"PDF parsing failed: {e}")

    elif file_ext == ".md":
        # Handle Markdown with proper parsing (#2667)
        try:
            import frontmatter
            from markdown import markdown
            from bs4 import BeautifulSoup

            # Parse frontmatter and content
            post = frontmatter.loads(content.decode("utf-8"))

            # Extract metadata from frontmatter
            metadata = dict(post.metadata) if post.metadata else {}

            # Convert markdown to plain text (strip HTML tags)
            # This preserves structure while making it searchable
            html = markdown(post.content)
            soup = BeautifulSoup(html, 'html.parser')
            text = soup.get_text(separator='\n\n')

            # Prepend frontmatter as metadata section
            if metadata:
                meta_str = "\n".join(f"{k}: {v}" for k, v in metadata.items())
                text = f"[Metadata]\n{meta_str}\n\n{text}"

        except ImportError:
            # Fallback to plain text if libraries unavailable
            text = content.decode("utf-8")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Markdown parsing failed: {e}")

    elif file_ext == ".json":
        # JSON files stored as formatted text
        import json
        try:
            data = json.loads(content.decode("utf-8"))
            text = json.dumps(data, indent=2, ensure_ascii=False)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON file: {e}")

    else:
        # Plain text
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as e:
            raise HTTPException(status_code=400, detail=f"File encoding error: {e}")

    # Use filename as doc_id if not provided (sanitize to prevent path traversal)
    safe_filename = os.path.basename(file.filename or "").strip() if file.filename else ""
    final_doc_id = doc_id or safe_filename or f"upload_{uuid.uuid4().hex[:8]}"

    # Ingest
    try:
        chunks = ingest_document(doc_id=final_doc_id, text=text)
        return RagIngestResponse(doc_id=final_doc_id, chunks_ingested=chunks)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}")


app.include_router(api)