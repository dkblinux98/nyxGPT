from __future__ import annotations

import inspect
import io
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.request
import uuid
from configparser import ConfigParser
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from fastapi import (
    APIRouter,
    Body,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.status import HTTP_401_UNAUTHORIZED

import nyxgpt.config
from nyxgpt import admin_activity as admin_activity_module
from nyxgpt import api_models, models, sessions, tools_fs
from nyxgpt import canary as canary_module
from nyxgpt import chat as chat_module
from nyxgpt import deploy as deploy_module
from nyxgpt import error_tracking as error_tracking_module
from nyxgpt import health as health_module
from nyxgpt import metrics as prom_metrics
from nyxgpt import self_heal as self_heal_module
from nyxgpt import tracing as tracing_module
from nyxgpt import usage_analytics as usage_analytics_module
from nyxgpt import workflow_analytics as workflow_analytics_module
from nyxgpt.api_models import (
    AttachDocumentRequest,
    ChatRequest,
    ChatResponse,
    CollectionDeleteResponse,
    CollectionInfo,
    CollectionSettings,
    CollectionSettingsResponse,
    CollectionsListResponse,
    CreateCollectionRequest,
    CreateCollectionResponse,
    InfoResponse,
    QueryCacheClearResponse,
    QueryCacheStatsResponse,
    RagChunkInfo,
    RagDocumentInfo,
    RagIndexRepoRequest,
    RagIndexRepoResponse,
    RagIngestRequest,
    RagIngestResponse,
    RagMetricsQueryRequest,
    RagMetricsQueryResponse,
    RagQueryRequest,
    RagQueryResponse,
    RagQueryResult,
    ReindexCollectionRequest,
    ReindexCollectionResponse,
    RenameRequest,
    ResourceMetricsResponse,
    SessionDocumentsResponse,
    SessionsListResponse,
    TagsRequest,
    TitleRequest,
    ToolCatRequest,
    ToolGrepRequest,
    ToolLsRequest,
    ToolTextResponse,
)
from nyxgpt.batch_processor import BatchProcessor, RequestPriority
from nyxgpt.chat import chat as run_chat
from nyxgpt.chat import chat_stream
from nyxgpt.config import (
    get_canary_error_rate_threshold,
    get_canary_latency_p95_threshold_ms,
    get_canary_min_requests,
    get_canary_namespace,
    get_canary_step_percent,
    get_canary_total_replicas,
    get_chat_timeout_seconds,
    get_default_model,
    get_deploy_namespace,
    get_error_tracking_config,
    get_log_aggregation_config,
    get_monitoring_config,
    get_ollama_base_url,
    get_rag_enabled,
    get_rag_good_score_threshold,
    get_rag_medium_score_threshold,
    get_rag_min_score,
    get_rate_limit_config,
    get_rate_limit_enabled,
    get_self_heal_backoff_seconds,
    get_self_heal_check_interval_seconds,
    get_self_heal_default_enabled,
    get_self_heal_max_consecutive_restarts,
    get_sessions_dir,
    get_tools_root,
    get_tracing_config,
    load_config,
)
from nyxgpt.logging import configure_logging, get_log_dir, request_id_var
from nyxgpt.ollama_client import ModelRuntimeError
from nyxgpt.rag.rag import (
    clear_query_cache,
    get_query_cache_stats,
    ingest_document,
    retrieve_context,
)
from nyxgpt.rate_limiter import RateLimiter
from nyxgpt.resource_monitor import ResourceMonitor, get_resource_monitor, init_resource_monitor
from nyxgpt.token_counter import count_tokens as _count_usage_tokens

log = logging.getLogger("nyxgpt.api")

# Global rate limiter instance (initialized at startup if enabled)
_rate_limiter: RateLimiter | None = None

# Global batch processor instance (initialized at startup if enabled)
_batch_processor: BatchProcessor[dict, dict] | None = None

# Global resource monitor instance (initialized at startup)
_resource_monitor: ResourceMonitor | None = None


@dataclass
class ClientCapabilities:
    """Client capability hints for content negotiation.

    Attributes:
        supports_sse: Client supports Server-Sent Events (text/event-stream)
        supports_structured_events: Client supports structured event types (metadata, text, done, error)
        supports_streaming: Client supports streaming responses
        client_version: Optional client version string (e.g., "web-ui/1.0.0", "tui/1.0.0")
        max_event_size: Maximum event payload size in bytes (0 = unlimited)
    """

    supports_sse: bool = False
    supports_structured_events: bool = False
    supports_streaming: bool = True
    client_version: str | None = None
    max_event_size: int = 0


def _parse_client_capabilities(request: Request) -> ClientCapabilities:
    """Parse client capability hints from request headers.

    Examines standard and custom headers to determine client capabilities:
    - Accept: text/event-stream indicates SSE support
    - X-Client-Supports-SSE: Explicit SSE capability flag
    - X-Client-Supports-Structured-Events: Structured event support flag
    - X-Client-Version: Client version identifier
    - X-Client-Max-Event-Size: Maximum event size in bytes

    Args:
        request: FastAPI Request object

    Returns:
        ClientCapabilities with detected or default capabilities
    """
    capabilities = ClientCapabilities()

    # Check Accept header for text/event-stream
    accept = request.headers.get("accept", "")
    capabilities.supports_sse = "text/event-stream" in accept.lower()

    # Check explicit capability headers
    supports_sse_header = request.headers.get("x-client-supports-sse", "").lower()
    if supports_sse_header in ("true", "1", "yes"):
        capabilities.supports_sse = True

    supports_structured = request.headers.get("x-client-supports-structured-events", "").lower()
    if supports_structured in ("true", "1", "yes"):
        capabilities.supports_structured_events = True

    # Check for streaming support (default true)
    supports_streaming = request.headers.get("x-client-supports-streaming", "true").lower()
    capabilities.supports_streaming = supports_streaming not in ("false", "0", "no")

    # Get client version
    capabilities.client_version = request.headers.get("x-client-version") or None

    # Get max event size
    max_size_str = request.headers.get("x-client-max-event-size", "0")
    try:
        capabilities.max_event_size = int(max_size_str)
    except ValueError:
        capabilities.max_event_size = 0

    return capabilities


def log_with_context(level, message, request_id=None, **extra):
    """Helper for structured logging with consistent context."""
    extra_fields = {"request_id": request_id, **extra} if request_id else extra
    log.log(level, message, extra=extra_fields)


# ----------------------------
# Startup diagnostics using lifespan
# ----------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _rate_limiter, _batch_processor

    # Initialize centralized logging once for the API process
    cfg = load_config(None)
    try:
        configure_logging(cfg, console=False)
        log.info("Centralized logging initialized", extra={"component": "startup"})
    except Exception as e:
        # Logging should not prevent API startup
        print(f"Logging initialization failed: {e}")

    # Initialize distributed tracing (no-op unless [tracing] enabled = true)
    try:
        tracing_module.init_tracing(_app, get_tracing_config(cfg))
    except Exception as e:
        log.warning("Tracing initialization failed: %s", e, extra={"component": "startup"})

    # Initialize error tracking (no-op unless [error_tracking] enabled = true
    # and a local DSN is configured)
    try:
        error_tracking_module.init_error_tracking(get_error_tracking_config(cfg))
    except Exception as e:
        log.warning("Error tracking initialization failed: %s", e, extra={"component": "startup"})

    # Ensure sessions directory exists
    sessions_dir = get_sessions_dir(cfg)
    try:
        sessions_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "Sessions directory ready",
            extra={"component": "startup", "sessions_dir": str(sessions_dir)},
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
                extra={"component": "startup", "ollama_url": base_url},
            )
    except urllib.error.URLError as e:
        log.warning(
            "Ollama health check failed",
            extra={
                "component": "startup",
                "ollama_url": base_url,
                "error": str(e),
                "note": "API will still start; chat requests may fail until Ollama is available",
            },
        )

    # Initialize rate limiter if enabled
    if get_rate_limit_enabled(cfg):
        rate_cfg = get_rate_limit_config(cfg)
        _rate_limiter = RateLimiter(
            requests_per_second=rate_cfg["requests_per_second"],
            burst_size=rate_cfg["burst_size"],
        )
        log.info(
            "Rate limiting enabled",
            extra={
                "component": "startup",
                "requests_per_second": rate_cfg["requests_per_second"],
                "burst_size": rate_cfg["burst_size"],
            },
        )
    else:
        log.info("Rate limiting disabled", extra={"component": "startup"})

    # Initialize batch processor if enabled
    from nyxgpt.config import get_batch_enabled, get_batch_size, get_batch_wait_time_ms

    if get_batch_enabled(cfg):
        batch_size = get_batch_size(cfg)
        wait_time_ms = get_batch_wait_time_ms(cfg)

        # Define batch processing function
        def _process_chat_batch(requests):
            """Process a batch of chat requests together."""
            results = []
            for batch_req in requests:
                try:
                    # Extract request data
                    req_data = batch_req.data
                    result = run_chat(
                        req_data["prompt"],
                        session=req_data["session"],
                        new=req_data["new"],
                        model=req_data["model"],
                        system=req_data["system"],
                        config_path=req_data["config_path"],
                        sessions_dir=req_data["sessions_dir"],
                        rag_enabled=req_data.get("rag_enabled"),
                        rag_filters=req_data.get("rag_filters"),
                    )
                    # Convert ChatResult to dict
                    result_dict = {
                        "session": result.session,
                        "model": result.model,
                        "reply": result.reply,
                        "rag_used": result.rag_used,
                        "rag_chunks": result.rag_chunks,
                        "rag_context": result.rag_context,
                    }
                    results.append(result_dict)
                except Exception as e:
                    # On error, append error dict
                    results.append({"error": str(e), "error_type": type(e).__name__})

            return results

        _batch_processor = BatchProcessor(
            batch_size=batch_size,
            wait_time_ms=wait_time_ms,
            process_fn=_process_chat_batch,
        )
        _batch_processor.start()

        log.info(
            "Request batching enabled",
            extra={
                "component": "startup",
                "batch_size": batch_size,
                "wait_time_ms": wait_time_ms,
            },
        )
    else:
        log.info("Request batching disabled", extra={"component": "startup"})

    # Initialize resource monitor
    global _resource_monitor
    _resource_monitor = init_resource_monitor(batch_processor=_batch_processor)

    # Start the self-heal watchdog. It's always running so the SRE/admin
    # dashboard's toggle takes effect immediately without an API restart --
    # it only takes action when enabled (seeded from config.ini on a fresh
    # install, then controlled entirely by the dashboard toggle).
    self_heal_module.seed_enabled_default(get_self_heal_default_enabled(cfg))
    watchdog = self_heal_module.get_watchdog()
    watchdog.interval_seconds = get_self_heal_check_interval_seconds(cfg)
    watchdog.max_consecutive_restarts = get_self_heal_max_consecutive_restarts(cfg)
    watchdog.backoff_seconds = get_self_heal_backoff_seconds(cfg)
    watchdog.start()
    log.info("Self-heal watchdog initialized", extra={"component": "startup"})

    yield

    # Cleanup: stop batch processor
    if _batch_processor:
        _batch_processor.stop()
        log.info("Batch processor stopped", extra={"component": "shutdown"})

    self_heal_module.get_watchdog().stop()
    log.info("Self-heal watchdog stopped", extra={"component": "shutdown"})


# Versioned API router
app = FastAPI(title="nyxGPT", version="1.0.0.md", lifespan=lifespan)
api = APIRouter(prefix="/api/v1")


# CORS: default to local-only origins (configurable via NYXGPT_CORS_ORIGINS)
# Example: export NYXGPT_CORS_ORIGINS="http://127.0.0.1:3000,http://localhost:3000"
_origins_env = os.environ.get("NYXGPT_CORS_ORIGINS", "").strip()
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
        "style-src 'self' 'unsafe-inline'; "  # Allow inline styles
        "img-src 'self' data:; "  # Allow data URIs for images
        "connect-src 'self'; "  # API calls to same origin only
        "frame-ancestors 'none'; "  # Prevent embedding (redundant with X-Frame-Options)
        "form-action 'self'; "  # Forms can only submit to same origin
        "base-uri 'self'"  # Prevent base tag injection
    )

    # Prevent MIME type sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"

    # Prevent clickjacking by disallowing framing
    response.headers["X-Frame-Options"] = "DENY"

    # Strict-Transport-Security (HSTS) - only for HTTPS
    # Force browser to use HTTPS for 1 year
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    return response


MAX_BODY_BYTES = int(os.environ.get("NYXGPT_MAX_BODY_BYTES", "1048576"))  # 1 MiB default


# Middleware to load config and hot-apply logging on every request
@app.middleware("http")
async def load_cfg_and_refresh_logging(request: Request, call_next):
    """Load config for this request and hot-apply logging level.

    We want edits to ~/.nyxGPT/config.ini (model, rag enabled, log level, auth, etc.)
    to take effect without restarting the API process.

    The loaded config is stored on request.state.cfg for reuse by downstream
    middleware/handlers.
    """

    cfg = load_config(None)
    request.state.cfg = cfg

    # Hot-apply logging config (especially level) on every request.
    # configure_logging() is expected to be idempotent and cheap.
    # Never block request handling on logging reconfiguration.
    with suppress(Exception):
        configure_logging(cfg, console=False)

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
            req_id,
        )

        # Return 429 error with rate limit headers
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "code": "rate_limit_exceeded",
                    "message": "Too many requests. Please try again later.",
                    "request_id": req_id,
                }
            },
            headers=headers,
        )

    # Add rate limit headers to response
    response = await call_next(request)
    for key, value in headers.items():
        response.headers[key] = value

    return response


@app.middleware("http")
async def request_latency_middleware(request: Request, call_next):
    """Track request latency for resource monitoring.

    Measures total request processing time and records it in the resource monitor.
    """
    start_time = time.time()

    # Process request
    response = await call_next(request)

    # Calculate latency
    latency_ms = (time.time() - start_time) * 1000

    # Record in resource monitor
    monitor = get_resource_monitor()
    if monitor is not None:
        monitor.record_request_latency(latency_ms, is_error=response.status_code >= 500)

    return response


@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    path = request.url.path

    # Allow unauthenticated access to health and docs
    if (
        path == "/health"
        or path.startswith("/docs")
        or path.startswith("/openapi")
        or path.startswith("/redoc")
    ):
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


@app.middleware("http")
async def prometheus_metrics_middleware(request: Request, call_next):
    """Record Prometheus HTTP metrics for every request.

    Registered after api_key_auth so it becomes the outermost middleware,
    ensuring it observes the final response status even for requests
    rejected by auth, rate limiting, or the body-size guard.
    """
    start_time = time.time()
    response = await call_next(request)
    duration_s = time.time() - start_time

    route = request.scope.get("route")
    path_label = route.path if route is not None else request.url.path
    status_label = str(response.status_code)

    prom_metrics.HTTP_REQUESTS_TOTAL.labels(
        method=request.method, path=path_label, status=status_label
    ).inc()
    prom_metrics.HTTP_REQUEST_DURATION_SECONDS.labels(
        method=request.method, path=path_label
    ).observe(duration_s)
    if response.status_code >= 500:
        prom_metrics.HTTP_ERRORS_TOTAL.labels(method=request.method, path=path_label).inc()

    return response


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
async def unhandled_exception_handler(request: Request, _exc: Exception):
    req_id = getattr(request.state, "request_id", None)
    log.exception("Unhandled API error (request_id=%s)", req_id)
    error_tracking_module.capture_exception(
        _exc,
        request_id=req_id or "N/A",
        path=request.url.path,
        method=request.method,
    )
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


# Auth config is read on each request so ~/.nyxGPT/config.ini edits
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
    return Path.home() / ".nyxGPT" / "config.ini"


def _apply_hot_config_updates(updates: dict[str, Any]) -> dict[str, Any]:
    """Apply a small set of hot config updates to ~/.nyxGPT/config.ini.

    Supported updates:
    - default_model (str) -> [ollama] default_model
    - rag_enabled (bool)  -> [rag] enable_chat_context
    - log_level (str)     -> [logging] level

    After writing, reload config and re-configure logging so log level changes apply immediately.
    Model and RAG are read per-request by chat endpoints, so they apply immediately as well.
    """

    cfg_path = _config_file_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    parser = ConfigParser()
    # Preserve key case (ConfigParser lowercases by default)
    parser.optionxform = str  # type: ignore[assignment]
    if cfg_path.exists():
        parser.read(cfg_path)

    def ensure_section(name: str) -> None:
        if not parser.has_section(name):
            parser.add_section(name)

    out: dict[str, Any] = {}

    if "default_model" in updates and isinstance(updates.get("default_model"), str):
        ensure_section("nyxgpt")
        parser.set("nyxgpt", "default_model", updates["default_model"].strip())
        out["default_model"] = updates["default_model"].strip()

    if "rag_enabled" in updates:
        ensure_section("rag")
        val = bool(updates["rag_enabled"])
        parser.set("rag", "enable_chat_context", "true" if val else "false")
        out["rag_enabled"] = val

    if "log_level" in updates and isinstance(updates.get("log_level"), str):
        ensure_section("logging")
        lvl = updates["log_level"].strip().upper()
        parser.set("logging", "level", lvl)
        out["log_level"] = lvl

    # Persist changes
    with cfg_path.open("w", encoding="utf-8") as f:
        parser.write(f)

    # Invalidate config cache to force reload on next access
    # This ensures mtime-based caching works even for rapid writes/reads
    nyxgpt.config._CACHED_CFG = None
    nyxgpt.config._CACHED_PATH = None
    nyxgpt.config._CACHED_MTIME_NS = None

    # Hot-apply logging changes immediately
    try:
        cfg = load_config(None)
        configure_logging(cfg, console=False)
    except Exception:
        # Do not fail the request if logging reconfig fails
        pass

    return out


def _apply_auth_config_updates(updates: dict[str, Any]) -> dict[str, Any]:
    """Apply auth-section updates to ~/.nyxGPT/config.ini.

    Supported updates:
    - enabled (bool) -> [auth] enabled
    - header (str)   -> [auth] header
    - api_key (str)  -> [auth] api_key

    Auth config is read fresh on every request (see `_auth_cfg`), so
    changes take effect immediately without a service restart.
    """

    cfg_path = _config_file_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    parser = ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    if cfg_path.exists():
        parser.read(cfg_path)

    if not parser.has_section("auth"):
        parser.add_section("auth")

    out: dict[str, Any] = {}

    if "enabled" in updates:
        val = bool(updates["enabled"])
        parser.set("auth", "enabled", "true" if val else "false")
        out["enabled"] = val

    if "header" in updates and isinstance(updates.get("header"), str):
        header = updates["header"].strip()
        parser.set("auth", "header", header)
        out["header"] = header

    if "api_key" in updates and isinstance(updates.get("api_key"), str):
        parser.set("auth", "api_key", updates["api_key"])
        out["api_key"] = updates["api_key"]

    with cfg_path.open("w", encoding="utf-8") as f:
        parser.write(f)

    nyxgpt.config._CACHED_CFG = None
    nyxgpt.config._CACHED_PATH = None
    nyxgpt.config._CACHED_MTIME_NS = None

    return out


def _mask_api_key(api_key: str) -> str:
    """Mask an API key for display, keeping only a few edge characters."""
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}{'*' * (len(api_key) - 8)}{api_key[-4:]}"


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
    rag_enabled = get_rag_enabled(cfg)
    return {
        "cfg": cfg,
        "default_model": get_default_model(cfg),
        "rag_enabled": rag_enabled,
        "chat_timeout_seconds": get_chat_timeout_seconds(cfg),
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


@app.get("/metrics")
def prometheus_metrics_endpoint() -> Response:
    """Expose metrics in Prometheus text exposition format for scraping.

    Includes HTTP request counts, latency histograms, and error rates
    (all endpoints) plus business metrics (chat requests, RAG queries).
    Left unauthenticated like /health since Prometheus scrapers typically
    don't send an API key.
    """
    body, content_type = prom_metrics.render_metrics()
    return Response(content=body, media_type=content_type)


@api.get("/info", response_model=InfoResponse)
def info(request: Request) -> InfoResponse:
    cfg = _req_cfg(request)
    release_version = cfg.get("github", "RELEASE_BRANCH", fallback=None)
    return InfoResponse(
        ollama_base_url=get_ollama_base_url(cfg),
        default_model=get_default_model(cfg),
        sessions_dir=str(get_sessions_dir(cfg)),
        release_version=release_version,
    )


@api.get("/batch/metrics")
def batch_metrics() -> dict[str, Any]:
    """Get batch processing metrics.

    Returns:
        Dictionary with batch metrics including total requests, avg batch size,
        throughput, wait times, etc. Returns empty metrics if batching disabled.
    """
    if _batch_processor is None:
        return {
            "enabled": False,
            "message": "Request batching is not enabled",
        }

    metrics = _batch_processor.get_metrics()
    return {
        "enabled": True,
        **metrics.to_dict(),
    }


@api.get("/metrics", response_model=ResourceMetricsResponse)
def resource_metrics() -> ResourceMetricsResponse:
    """Get system resource usage metrics.

    Returns comprehensive resource monitoring including:
    - Memory usage (RSS, VMS, available)
    - CPU utilization (process and system)
    - Request latency (avg, p50, p95, p99)
    - Queue depth (current and total requests)

    Returns:
        ResourceMetricsResponse with current resource metrics
    """
    monitor = get_resource_monitor()
    if monitor is None:
        # Return zero metrics if monitor not initialized
        return ResourceMetricsResponse(
            memory={"rss_mb": 0, "vms_mb": 0, "percent": 0, "available_mb": 0},
            cpu={"process_percent": 0, "system_percent": 0},
            latency={"avg_ms": 0, "p50_ms": 0, "p95_ms": 0, "p99_ms": 0},
            queue={"depth": 0, "total_requests": 0},
        )

    metrics = monitor.get_metrics()
    return ResourceMetricsResponse(**metrics.to_dict())


@api.get("/tracing")
def tracing_status(request: Request) -> dict[str, Any]:
    """Get distributed tracing status and how to reach the local Jaeger UI.

    Tracing is opt-in and local-only: enable it with `[tracing] enabled =
    true` in config.ini and start the API alongside the `tracing` Compose
    profile (local OTel collector + Jaeger all-in-one). No spans are ever
    exported outside the machine.
    """
    cfg = _req_cfg(request)
    tracing_config = get_tracing_config(cfg)
    return {
        **tracing_config,
        "active": tracing_module.is_tracing_enabled(),
    }


@api.get("/error-tracking")
def error_tracking_status(request: Request) -> dict[str, Any]:
    """Get error tracking status and how to reach the local GlitchTip UI.

    Error tracking is opt-in and local-only: enable it with
    `[error_tracking] enabled = true` and a `dsn` pointed at a self-hosted
    tracker in config.ini, then start the API alongside the `errors`
    Compose profile (local GlitchTip instance). No exception data is ever
    sent to Sentry's own SaaS.
    """
    cfg = _req_cfg(request)
    error_tracking_config = get_error_tracking_config(cfg)
    return {
        **error_tracking_config,
        "active": error_tracking_module.is_error_tracking_enabled(),
    }


@api.post("/error-tracking/report")
def error_tracking_report(request: Request, body: api_models.ClientErrorReportRequest) -> Response:
    """Forward a web UI client-side error to the local error tracker.

    Returns `503 {"status": "inactive"}` when error tracking isn't actually
    initialized (disabled, or enabled with no valid DSN), rather than a
    blanket `202` -- a misconfigured DSN or a tracker nobody enabled must not
    look like a successfully delivered test event. The web UI's fire-and-forget
    client error reporter (`ClientErrorReporter.tsx`) ignores the response
    either way, so this stays safe to call whether or not tracking is active.
    """
    if not error_tracking_module.is_error_tracking_enabled():
        return JSONResponse(
            status_code=503,
            content={
                "status": "inactive",
                "detail": "Error tracking is disabled or has no valid DSN configured; "
                "this event was not sent.",
            },
        )

    req_id = getattr(request.state, "request_id", None)
    error_tracking_module.capture_message(
        body.message,
        request_id=req_id or "N/A",
        source="web-client",
        url=body.url or "N/A",
        stack=body.stack or "N/A",
    )
    return JSONResponse(status_code=202, content={"status": "accepted"})


@api.get("/monitoring")
def monitoring_status(request: Request) -> dict[str, Any]:
    """Get monitoring stack status and how to reach the local Grafana UI.

    The Grafana/Prometheus stack is opt-in and local-only: enable it with
    `[monitoring] enabled = true` in config.ini and start the API alongside
    the `monitoring` Compose profile (local Prometheus + Grafana). No
    metrics are ever exported outside the machine.
    """
    cfg = _req_cfg(request)
    monitoring_config = get_monitoring_config(cfg)
    return {
        **monitoring_config,
        "active": monitoring_config["enabled"],
    }


@api.get("/log-aggregation")
def log_aggregation_status(request: Request) -> dict[str, Any]:
    """Get log aggregation status and how to reach the local Loki search UI.

    Log aggregation is opt-in and local-only: enable it with
    `[log_aggregation] enabled = true` in config.ini and start the API
    alongside the `logging` Compose profile (local Loki + promtail).
    Promtail ships the API's log files under ~/.nyxGPT/logs into Loki, which
    is searched via Grafana Explore (the `monitoring` profile). No logs are
    ever exported outside the machine.
    """
    cfg = _req_cfg(request)
    log_aggregation_config = get_log_aggregation_config(cfg)
    return {
        **log_aggregation_config,
        "active": log_aggregation_config["enabled"],
    }


# --- Config get/set endpoints ---


@api.get("/config")
def config_get(request: Request) -> dict[str, Any]:
    cfg = _req_cfg(request)
    return {
        "ollama_base_url": get_ollama_base_url(cfg),
        "default_model": get_default_model(cfg),
        "rag_enabled": get_rag_enabled(cfg),
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
    if changed:
        admin_activity_module.record(
            "config.updated", ", ".join(f"{k}={v}" for k, v in changed.items())
        )

    # After applying, reload config and update request.state.cfg
    request.state.cfg = load_config(None)
    cfg = _req_cfg(request)
    return {
        "updated": changed,
        "effective": {
            "ollama_base_url": get_ollama_base_url(cfg),
            "default_model": get_default_model(cfg),
            "rag_enabled": get_rag_enabled(cfg),
            "log_level": cfg.get("logging", "level", fallback="INFO").strip().upper(),
        },
    }


# PATCH endpoint for config updates
@api.patch("/config")
def config_patch(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    result: dict[str, Any] = config_update(request, payload)
    return result


# --- Admin dashboard endpoints (system status overview, activity log, access) ---


@api.get("/admin/overview")
def admin_overview(request: Request) -> dict[str, Any]:
    """Aggregate system status for the admin dashboard overview panel.

    Combines app info, resource metrics, deploy/canary status, and the
    enabled/disabled state of opt-in observability stacks into a single
    response so the dashboard can render a status summary in one request.
    Individual sub-sections degrade to an `{"error": ...}` payload instead
    of failing the whole request when a backing service (e.g. a local K8s
    cluster for deploy/canary) is unavailable.
    """
    cfg = _req_cfg(request)

    def _safe(fn, *args, **kwargs) -> dict[str, Any]:
        try:
            result: dict[str, Any] = fn(*args, **kwargs)
            return result
        except Exception as e:
            return {"error": str(e)}

    monitor = get_resource_monitor()
    resource_metrics_summary = monitor.get_metrics().to_dict() if monitor is not None else None

    return {
        "info": {
            "ollama_base_url": get_ollama_base_url(cfg),
            "default_model": get_default_model(cfg),
            "rag_enabled": get_rag_enabled(cfg),
        },
        "resource_metrics": resource_metrics_summary,
        "deploy": _safe(deploy_module.status, get_deploy_namespace(cfg)),
        "canary": _safe(canary_module.status, get_canary_namespace(cfg)),
        "self_heal": _safe(self_heal_module.status),
        "observability": {
            "monitoring": get_monitoring_config(cfg)["enabled"],
            "tracing": get_tracing_config(cfg)["enabled"],
            "error_tracking": get_error_tracking_config(cfg)["enabled"],
            "log_aggregation": get_log_aggregation_config(cfg)["enabled"],
        },
        "auth_enabled": _auth_cfg(cfg)["enabled"],
    }


@api.get("/admin/health")
def admin_health(request: Request) -> dict[str, Any]:
    """Aggregate system health for the admin health dashboard.

    Combines service uptime, dependency reachability checks (Ollama, and
    Cassandra when RAG is enabled), resource utilization, and
    threshold-based alert indicators into a single response.
    """
    cfg = _req_cfg(request)
    rag_enabled = get_rag_enabled(cfg)

    monitor = get_resource_monitor()
    resource_metrics_summary = monitor.get_metrics().to_dict() if monitor is not None else None

    dependencies = [
        health_module.check_ollama(get_ollama_base_url(cfg)),
        health_module.check_cassandra(rag_enabled),
    ]
    alerts = health_module.compute_alerts(resource_metrics_summary, dependencies)

    return {
        "service": {"status": "ok", "uptime_s": round(health_module.uptime_seconds(), 1)},
        "dependencies": [d.to_dict() for d in dependencies],
        "resource_metrics": resource_metrics_summary,
        "alerts": [a.to_dict() for a in alerts],
    }


@api.get("/admin/activity")
def admin_activity_list(request: Request, limit: int = 50) -> dict[str, Any]:
    """Return recent admin dashboard activity (audit trail)."""
    cfg = _req_cfg(request)
    bounded_limit = max(1, min(limit, 500))
    return {"events": admin_activity_module.recent(bounded_limit, cfg=cfg)}


@api.get("/admin/access")
def admin_access_get(request: Request) -> dict[str, Any]:
    """Return the current API-key access configuration (key masked)."""
    cfg = _req_cfg(request)
    auth = _auth_cfg(cfg)
    return {
        "enabled": auth["enabled"],
        "header": auth["header"],
        "api_key_set": bool(auth["api_key"]),
        "api_key_masked": _mask_api_key(auth["api_key"]) if auth["api_key"] else None,
    }


@api.post("/admin/access")
def admin_access_update(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """Update API-key access configuration: enable/disable auth, change the
    header name, or rotate the API key.

    On rotation, the newly generated key is returned once in the response
    body (`api_key`) so the operator can copy it; it is never returned
    again by `GET /admin/access`, which only shows a masked value.
    """
    cfg = _req_cfg(request)
    auth = _auth_cfg(cfg)

    updates: dict[str, Any] = {}
    if "enabled" in payload:
        updates["enabled"] = bool(payload["enabled"])
    if "header" in payload and isinstance(payload.get("header"), str) and payload["header"].strip():
        updates["header"] = payload["header"]

    rotate = bool(payload.get("rotate"))
    new_key: str | None = None
    if rotate:
        new_key = secrets.token_urlsafe(32)
        updates["api_key"] = new_key

    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    _apply_auth_config_updates(updates)

    request.state.cfg = load_config(None)
    cfg = _req_cfg(request)
    auth = _auth_cfg(cfg)

    changes = []
    if "enabled" in updates:
        changes.append("enabled" if updates["enabled"] else "disabled")
    if "header" in updates:
        changes.append("header changed")
    if rotate:
        changes.append("API key rotated")
    admin_activity_module.record("access.updated", ", ".join(changes) or "access settings updated")

    result: dict[str, Any] = {
        "enabled": auth["enabled"],
        "header": auth["header"],
        "api_key_set": bool(auth["api_key"]),
        "api_key_masked": _mask_api_key(auth["api_key"]) if auth["api_key"] else None,
    }
    if new_key is not None:
        result["api_key"] = new_key
    return result


@api.get("/analytics/usage")
def analytics_usage(request: Request) -> dict[str, Any]:
    """Return aggregated usage analytics for the admin dashboard.

    Summarizes recorded chat requests: total requests/tokens, distinct
    session count, and per-model and per-day breakdowns.
    """
    cfg = _req_cfg(request)
    return usage_analytics_module.summary(cfg=cfg)


@api.get("/analytics/export")
def analytics_export(request: Request, format: str = "json") -> Response:
    """Export recorded usage analytics as a downloadable JSON or CSV report."""
    cfg = _req_cfg(request)
    try:
        content, content_type, filename = usage_analytics_module.export_report(format, cfg=cfg)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@api.get("/admin/workflow-analytics")
def admin_workflow_analytics(days: int = 30, limit: int = 50) -> dict[str, Any]:
    """CI/CD workflow run history and analytics for the admin dashboard.

    Wraps the SQLite store built by `scripts/collect_workflow_logs.py collect`
    (success rate, avg duration, per-workflow/day breakdowns, top failing
    workflows, and recent runs) so it's visible from the dashboard, not just
    the CLI.
    """
    return workflow_analytics_module.summary(days=days, limit=limit)


# --- Local blue/green deployment endpoints (SRE/admin dashboard) ---
@api.get("/deploy/status")
def deploy_status(request: Request) -> dict[str, Any]:
    cfg = _req_cfg(request)
    return deploy_module.status(get_deploy_namespace(cfg))


@api.post("/deploy/switch")
def deploy_switch(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    cfg = _req_cfg(request)
    target = payload.get("to")
    if target is not None and target not in deploy_module.COLORS:
        raise HTTPException(status_code=400, detail=f"Invalid color: {target!r}")
    result = deploy_module.switch(
        target=target, namespace=get_deploy_namespace(cfg), force=bool(payload.get("force", False))
    )
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message)
    admin_activity_module.record("deploy.switch", result.message)
    return {"ok": result.ok, "message": result.message}


@api.post("/deploy/rollback")
def deploy_rollback(request: Request) -> dict[str, Any]:
    cfg = _req_cfg(request)
    result = deploy_module.rollback(get_deploy_namespace(cfg))
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message)
    admin_activity_module.record("deploy.rollback", result.message)
    return {"ok": result.ok, "message": result.message}


# --- Local canary deployment endpoints (SRE/admin dashboard) ---
@api.get("/canary/status")
def canary_status(request: Request) -> dict[str, Any]:
    cfg = _req_cfg(request)
    return canary_module.status(get_canary_namespace(cfg))


@api.post("/canary/start")
def canary_start(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    cfg = _req_cfg(request)
    weight_percent = int(payload.get("weight_percent", 10))
    result = canary_module.start(
        namespace=get_canary_namespace(cfg),
        weight_percent=weight_percent,
        total_replicas=get_canary_total_replicas(cfg),
    )
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message)
    admin_activity_module.record("canary.start", result.message)
    return {"ok": result.ok, "message": result.message}


@api.post("/canary/evaluate")
def canary_evaluate(request: Request) -> dict[str, Any]:
    cfg = _req_cfg(request)
    result = canary_module.evaluate(
        get_canary_namespace(cfg),
        error_rate_threshold_percent=get_canary_error_rate_threshold(cfg),
        latency_p95_threshold_ms=get_canary_latency_p95_threshold_ms(cfg),
        min_requests=get_canary_min_requests(cfg),
    )
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message)
    admin_activity_module.record("canary.evaluate", result.message)
    return {"ok": result.ok, "message": result.message}


@api.post("/canary/promote")
def canary_promote(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    cfg = _req_cfg(request)
    step_percent = payload.get("step_percent")
    result = canary_module.promote(
        namespace=get_canary_namespace(cfg),
        step_percent=(
            int(step_percent) if step_percent is not None else get_canary_step_percent(cfg)
        ),
        total_replicas=get_canary_total_replicas(cfg),
    )
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message)
    admin_activity_module.record("canary.promote", result.message)
    return {"ok": result.ok, "message": result.message}


@api.post("/canary/rollback")
def canary_rollback(request: Request) -> dict[str, Any]:
    cfg = _req_cfg(request)
    result = canary_module.rollback(
        namespace=get_canary_namespace(cfg), total_replicas=get_canary_total_replicas(cfg)
    )
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message)
    admin_activity_module.record("canary.rollback", result.message)
    return {"ok": result.ok, "message": result.message}


# --- Self-heal watchdog endpoints (SRE/admin dashboard) ---
@api.get("/self-heal/status")
def self_heal_status(_request: Request) -> dict[str, Any]:
    """Per-component health of the Docker Compose stack, plus recent heal events."""
    return self_heal_module.status()


@api.post("/self-heal/toggle")
def self_heal_toggle(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Enable/disable the self-heal watchdog from the SRE/admin dashboard."""
    if "enabled" not in payload:
        raise HTTPException(status_code=400, detail="Missing 'enabled' field")
    enabled = self_heal_module.set_enabled(bool(payload["enabled"]))
    admin_activity_module.record("self_heal.toggle", "enabled" if enabled else "disabled")
    return {"enabled": enabled}


@api.post("/self-heal/heal")
def self_heal_heal(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Manually trigger a heal pass -- one component (payload.service) or all of them."""
    service = payload.get("service")
    result = self_heal_module.heal_now(service=service)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    for event in result.get("healed", []):
        admin_activity_module.record(
            "self_heal.restart",
            f"{event['service']}: {event['message']} ({event['reason']})",
        )
    return result


@api.get("/self-heal/logs")
def self_heal_logs(service: str, tail: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
    """Recent Docker Compose logs for one component, from the SRE/admin dashboard.

    Lets an operator read a container's console output (e.g. the GlitchTip
    container's first-account registration confirmation link, printed there by
    its console email backend) without running a raw `docker`/`docker compose`
    command themselves.
    """
    result = self_heal_module.component_logs(service, tail=tail)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.message)
    return {"service": service, "tail": tail, "logs": result.details}


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
        raise HTTPException(
            status_code=502, detail=f"Failed to list models from Ollama: {e}"
        ) from e


@api.post("/models/pull")
def models_pull(request: Request, payload: dict[str, Any] = Body(...)) -> Response:
    """Pull a model from Ollama.

    When ``stream=true`` in the request body, returns a Server-Sent Events (SSE)
    stream with progress events (``status``, ``completed``, ``total``, ``percent``).
    Each event is a JSON object sent as ``data: {...}\\n\\n``.

    When ``stream`` is omitted or ``false``, returns a single JSON response on
    completion.
    """
    import json as _json

    cfg = _req_cfg(request)
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(status_code=400, detail="Missing 'model'")
    model = model.strip()
    stream = bool(payload.get("stream", False))

    if stream:
        base_url = get_ollama_base_url(cfg)

        def _progress_generator():
            from nyxgpt.ollama_client import post_json_lines

            url = base_url.rstrip("/") + "/api/pull"
            pull_payload = {"name": model, "stream": True}
            try:
                for event in post_json_lines(url, pull_payload, timeout_s=600.0):
                    status = event.get("status", "")
                    completed = event.get("completed", 0)
                    total = event.get("total", 0)
                    percent = round(completed / total * 100, 1) if total > 0 else 0.0
                    data = {
                        "status": status,
                        "completed": completed,
                        "total": total,
                        "percent": percent,
                    }
                    yield f"data: {_json.dumps(data)}\n\n"
                admin_activity_module.record("model.pull", model)
                yield f"data: {_json.dumps({'status': 'success', 'ok': True, 'model': model})}\n\n"
            except Exception as exc:
                yield f"data: {_json.dumps({'error': str(exc)})}\n\n"

        return StreamingResponse(
            _progress_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        # Non-streaming pull; Ollama may take a while.
        data = _ollama_post_json(
            _ollama_url(cfg, "/api/pull"),
            {"name": model, "stream": False},
            timeout_s=600.0,
        )
        admin_activity_module.record("model.pull", model)
        return JSONResponse({"ok": True, "model": model, "result": data})
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to pull model via Ollama: {e}") from e


@api.delete("/models/{model_name}")
def models_delete(request: Request, model_name: str) -> dict[str, Any]:
    """Delete a model from Ollama."""
    cfg = _req_cfg(request)
    try:
        models.delete_model(model_name, base_url=get_ollama_base_url(cfg))
        admin_activity_module.record("model.delete", model_name)
        return {"ok": True, "model": model_name}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=502, detail=f"Failed to delete model via Ollama: {e}"
        ) from e


@api.get("/models/{model_name}/info")
def models_info(request: Request, model_name: str) -> dict[str, Any]:
    """Get detailed information about a model."""
    cfg = _req_cfg(request)
    try:
        info = models.show_model(model_name, base_url=get_ollama_base_url(cfg))
        return {"ok": True, "model": model_name, "info": info}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=502, detail=f"Failed to get model info via Ollama: {e}"
        ) from e


@api.get("/sessions", response_model=SessionsListResponse)
def sessions_list(sessions_dir: str | None = None) -> SessionsListResponse:
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
                "token_estimate": (
                    meta.get("token_estimate")
                    if isinstance(meta.get("token_estimate"), int)
                    else None
                ),
                "model": meta.get("model") if isinstance(meta.get("model"), str) else "",
            }
        )
    return SessionsListResponse(sessions=out)


def search_params(
    query: str = Query(..., min_length=1, description="Text to search for in messages"),
    case_sensitive: bool = Query(False, description="Whether to perform case-sensitive search"),
    role_filter: api_models.MessageRole | None = Query(
        None, description="Filter by message role (user, assistant, system)"
    ),
    session_filter: str | None = Query(None, description="Filter to specific session name"),
    limit: int = Query(50, ge=1, le=500, description="Maximum number of results to return"),
) -> api_models.SearchRequest:
    """Dependency function to validate search parameters using SearchRequest model.

    This ensures proper validation of all search parameters according to the
    SearchRequest Pydantic model definition, including enum validation for role_filter.
    """
    return api_models.SearchRequest(
        query=query,
        case_sensitive=case_sensitive,
        role_filter=role_filter,
        session_filter=session_filter,
        limit=limit,
    )


@api.get("/sessions/search", response_model=api_models.SearchResponse)
def sessions_search(
    params: api_models.SearchRequest = Depends(search_params),
    sessions_dir: str | None = None,
) -> api_models.SearchResponse:
    """Search for messages across all sessions or within a specific session.

    Args:
        params: Validated search parameters (query, filters, limit)
        sessions_dir: Optional sessions directory override

    Returns:
        SearchResponse containing matching messages with context
    """
    cfg = _cfg(None)
    effective_dir = _sessions_dir_from_str(sessions_dir) or get_sessions_dir(cfg)

    # Perform search
    # Convert enum to string for sessions module
    role_filter_str = params.role_filter.value if params.role_filter else None
    results = sessions.search_messages(
        query=params.query,
        sessions_dir=effective_dir,
        case_sensitive=params.case_sensitive,
        role_filter=role_filter_str,
        session_filter=params.session_filter,
        limit=params.limit,
    )

    # Convert to API models
    result_items = [
        api_models.SearchResultItem(
            session_name=r["session_name"],
            session_title=r.get("session_title"),
            message_index=r["message_index"],
            role=r["role"],
            content=r["content"],
            content_preview=r["content_preview"],
            timestamp=r.get("timestamp"),
            matches=r["matches"],
        )
        for r in results
    ]

    return api_models.SearchResponse(
        query=params.query,
        total_results=len(result_items),
        results=result_items,
    )


@api.get("/sessions/{name}")
def sessions_show(
    name: str,
    sessions_dir: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    sd = _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None))
    sf = sessions.session_file_for(name, sd or sessions.default_sessions_dir())
    mf = sessions.meta_file_for(sf)
    if not sf.exists():
        raise HTTPException(status_code=404, detail="No such session")

    # Validate pagination parameters (Medium Issue 4)
    if offset is not None and offset < 0:
        raise HTTPException(status_code=400, detail="offset must be non-negative")
    if limit is not None and limit < 0:
        raise HTTPException(status_code=400, detail="limit must be non-negative")

    # Use paginated loading to avoid loading all messages into memory (Critical Issue 1)
    if offset is not None or limit is not None:
        start = offset if offset is not None else 0
        msgs, total_count = sessions.load_session_messages_paginated(sf, start, limit)
    else:
        # No pagination requested - use regular load for backward compatibility
        all_msgs = sessions.load_session_messages(sf)
        msgs = all_msgs
        total_count = len(all_msgs)

    meta = sessions.load_session_meta(mf)
    return {
        "name": name,
        "messages": msgs,
        "meta": meta,
        "total": total_count,
        "offset": offset if offset is not None else 0,
        "limit": limit if limit is not None else total_count,
    }


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
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        # Other errors are internal server errors
        log.error("Failed to get session file path: %s", e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    if sf.exists():
        return {"ok": True, "name": name, "existed": True}

    system = req.get("system")
    if not isinstance(system, str) or not system:
        # Use config default (empty string if not set)
        system = cfg.get("nyxgpt", "system_prompt", fallback="")

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
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {"ok": True, "name": name, "existed": False}


@api.delete("/sessions/{name}")
def sessions_delete(name: str, sessions_dir: str | None = None) -> dict[str, Any]:
    ok = sessions.delete_session(
        name, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None))
    )
    if not ok:
        raise HTTPException(status_code=404, detail="No such session")
    return {"ok": True}


@api.post("/sessions/{name}/summarize")
def sessions_summarize(name: str, sessions_dir: str | None = None) -> dict[str, Any]:
    ok, msg = sessions.summarize_session(
        name, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None))
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@api.post("/sessions/{name}/pin")
def sessions_pin(name: str, sessions_dir: str | None = None) -> dict[str, Any]:
    ok, msg = sessions.set_pinned(
        name, True, _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None))
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@api.post("/sessions/{name}/unpin")
def sessions_unpin(name: str, sessions_dir: str | None = None) -> dict[str, Any]:
    ok, msg = sessions.set_pinned(
        name,
        False,
        _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)),
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@api.post("/sessions/{name}/title")
def sessions_title(name: str, req: TitleRequest, sessions_dir: str | None = None) -> dict[str, Any]:
    ok, msg = sessions.set_title(
        name,
        req.title,
        _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)),
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@api.post("/sessions/{name}/tags/add")
def sessions_tags_add(
    name: str, req: TagsRequest, sessions_dir: str | None = None
) -> dict[str, Any]:
    if not req.tags:
        raise HTTPException(status_code=400, detail="At least one tag is required")
    ok, msg = sessions.add_tags(
        name,
        req.tags,
        _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)),
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@api.post("/sessions/{name}/tags/remove")
def sessions_tags_remove(
    name: str, req: TagsRequest, sessions_dir: str | None = None
) -> dict[str, Any]:
    if not req.tags:
        raise HTTPException(status_code=400, detail="At least one tag is required")
    ok, msg = sessions.remove_tags(
        name,
        req.tags,
        _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None)),
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True}


@api.post("/sessions/{name}/rename")
def sessions_rename(
    name: str, req: RenameRequest, sessions_dir: str | None = None
) -> dict[str, Any]:
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
        success, status, new_name = sessions.sync_filename_with_title(
            name, _sessions_dir, force=True
        )
        if not success:
            raise HTTPException(
                status_code=500,
                detail=f"Title updated but filename sync failed: {status}",
            )

        return {
            "ok": True,
            "old_name": name,
            "new_name": new_name,
            "message": "Session renamed and filename synced",
        }
    else:
        # Mode 2: Direct rename (validate new name first)
        try:
            validated_name = sessions.validate_session_name(req.new_name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        # Use existing rename function
        ok, msg = sessions.rename_session(name, validated_name, _sessions_dir)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)

        return {
            "ok": True,
            "old_name": name,
            "new_name": validated_name,
            "message": "Session renamed",
        }


@api.post("/sessions/{name}/sync-filename")
def sessions_sync_filename(name: str, sessions_dir: str | None = None) -> dict[str, Any]:
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

    responses = {
        "no_title": {"ok": True, "message": "No title set, filename unchanged", "name": name},
        "no_change": {"ok": True, "message": "Filename already matches title", "name": name},
        "renamed": {
            "ok": True,
            "old_name": name,
            "new_name": new_name,
            "message": "Filename synced with title",
        },
    }
    return responses.get(status, {"ok": True, "message": status, "name": new_name})


@api.patch("/sessions/{name}/messages/{message_index}")
def edit_message(
    name: str,
    message_index: int,
    req: api_models.EditMessageRequest,
    sessions_dir: str | None = None,
) -> dict[str, Any]:
    """Edit a message in a session.

    By default, forks the conversation (truncates messages after the edited one).
    Set fork=false to edit without truncating.
    """
    _sessions_dir = _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None))

    ok, msg = sessions.edit_message(
        session_name=name,
        message_index=message_index,
        new_content=req.content,
        sessions_dir=_sessions_dir,
        fork=req.fork,
    )

    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    return {"ok": True, "message": msg}


@api.get("/sessions/{name}/messages/{message_index}/rag")
def get_message_rag_chunks(
    name: str, message_index: int, sessions_dir: str | None = None
) -> dict[str, Any]:
    """Get RAG chunks for a specific message.

    Returns the RAG chunks associated with a message if available.
    This endpoint enables lazy loading of RAG citation data.
    """
    _sessions_dir = _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None))

    # Load session messages
    sf = sessions.session_file_for(name, _sessions_dir)
    if not sf.exists():
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")

    msgs = sessions.load_session_messages(sf)

    # Validate message index
    if message_index < 0 or message_index >= len(msgs):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid message_index: {message_index} (session has {len(msgs)} messages)",
        )

    message = msgs[message_index]

    # Extract RAG chunks if present
    rag_chunks: list[Any] = cast(list[Any], message.get("rag_chunks", []))

    return {
        "message_index": message_index,
        "has_rag": len(rag_chunks) > 0,
        "chunks": rag_chunks,
    }


def _escape_markdown(text: str) -> str:
    """Escape markdown special characters in text."""
    # Escape common markdown special characters that could break formatting
    replacements = {
        "\\": "\\\\",  # Backslash must be first
        "`": "\\`",
        "*": "\\*",
        "_": "\\_",
        "{": "\\{",
        "}": "\\}",
        "[": "\\[",
        "]": "\\]",
        "(": "\\(",
        ")": "\\)",
        "#": "\\#",
        "+": "\\+",
        "-": "\\-",
        ".": "\\.",
        "!": "\\!",
        "|": "\\|",
    }
    for char, escaped in replacements.items():
        text = text.replace(char, escaped)
    return text


@api.get("/sessions/{name}/citations/export", response_model=None)
def export_session_citations(
    name: str, format: str = "json", sessions_dir: str | None = None
) -> dict[str, Any] | Response:
    """Export all RAG citations from a session.

    Returns all RAG citations from assistant messages in the session.
    Useful for generating bibliographies or reference lists.

    Supported formats: json, markdown
    """
    # Validate session name to prevent path traversal attacks
    if not name or ".." in name or "/" in name or "\\" in name:
        raise HTTPException(
            status_code=400,
            detail="Invalid session name. Must not contain path separators or navigation characters.",
        )

    format_lower = format.lower()
    if format_lower not in ("json", "markdown"):
        raise HTTPException(
            status_code=400,
            detail="Invalid format. Must be one of: json, markdown",
        )

    _sessions_dir = _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None))

    # Load session messages
    sf = sessions.session_file_for(name, _sessions_dir)
    if not sf.exists():
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")

    # Use file locking to prevent race conditions during read
    with sessions.file_lock(sf, timeout=5.0):
        msgs = sessions.load_session_messages(sf)

    # Extract all citations from assistant messages
    citations: list[dict[str, Any]] = []
    for msg_idx, msg in enumerate(msgs):
        if msg.get("role") == "assistant":
            rag_chunks: list[Any] = cast(list[Any], msg.get("rag_chunks", []))
            if rag_chunks and isinstance(rag_chunks, list):
                for chunk_idx, chunk in enumerate(rag_chunks):
                    citations.append(
                        {
                            "message_index": msg_idx,
                            "citation_index": chunk_idx,
                            "doc_id": chunk.get("doc_id"),
                            "chunk_id": chunk.get("chunk_id"),
                            "text": chunk.get("text"),
                            "score": chunk.get("score"),
                            "similarity_score": chunk.get("similarity_score"),
                        }
                    )

    if format_lower == "json":
        return {
            "session": name,
            "total_citations": len(citations),
            "citations": citations,
        }
    else:  # markdown
        lines: list[str] = []
        lines.append(f"# Citations for {name}\n")
        lines.append(f"Total sources: {len(citations)}\n")
        lines.append("---\n")

        for idx, citation in enumerate(citations, 1):
            doc_id = _escape_markdown(citation.get("doc_id", "Unknown"))
            chunk_id = citation.get("chunk_id")
            # Use explicit None checking to avoid treating 0.0 as falsy
            score = citation.get("similarity_score")
            if score is None:
                score = citation.get("score", 0.0)
            text = _escape_markdown(citation.get("text", ""))

            chunk_ref = f"chunk {chunk_id}" if chunk_id is not None else "source"
            lines.append(f"## [{idx}] {doc_id} ({chunk_ref})\n")
            lines.append(f"**Confidence:** {score:.3f}")
            lines.append(f"**Message:** {citation['message_index']}\n")

            if text:
                lines.append(f"**Source text:**\n> {text}\n")

        return Response(
            content="\n".join(lines),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}-citations.md"'},
        )


@api.post("/sessions/{name}/messages/{message_index}/regenerate")
def regenerate_response(
    name: str,
    message_index: int,
    req: api_models.RegenerateRequest,
    sessions_dir: str | None = None,
) -> dict[str, Any]:
    """Regenerate response from a specific message.

    The message at message_index should be a user message.
    This will:
    1. Optionally replace the user message with new prompt (if provided)
    2. Truncate conversation after that message
    3. Generate a new response using the chat endpoint

    Returns the new response.
    """
    _sessions_dir = _sessions_dir_from_str(sessions_dir) or get_sessions_dir(_cfg(None))

    # Load session to validate message index
    sf = sessions.session_file_for(name, _sessions_dir)
    if not sf.exists():
        raise HTTPException(status_code=404, detail="No such session")

    msgs = sessions.load_session_messages(sf)
    if message_index < 0 or message_index >= len(msgs):
        raise HTTPException(status_code=400, detail=f"Invalid message index: {message_index}")

    message = msgs[message_index]
    if message.get("role") != "user":
        raise HTTPException(status_code=400, detail="Can only regenerate from user messages")

    # If new prompt provided, edit the message first
    prompt = req.prompt if req.prompt else message.get("content", "")
    if req.prompt:
        ok, msg = sessions.edit_message(
            session_name=name,
            message_index=message_index,
            new_content=req.prompt,
            sessions_dir=_sessions_dir,
            fork=True,  # Always fork when regenerating
        )
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
    else:
        # Just truncate after this message
        ok, msg = sessions.truncate_after_message(
            session_name=name,
            message_index=message_index,
            sessions_dir=_sessions_dir,
        )
        if not ok:
            raise HTTPException(status_code=400, detail=msg)

    # Generate new response using existing chat endpoint logic
    try:
        result = chat_module.chat(
            prompt=prompt,
            session=name,
            new=False,
            model=req.model,
            config_path=None,
            sessions_dir=str(_sessions_dir) if _sessions_dir else None,
            rag_enabled=req.rag_enabled,
        )
        return {
            "ok": True,
            "session": result.session,
            "model": result.model,
            "reply": result.reply,
            "rag_used": result.rag_used,
        }
    except Exception as e:
        log.error(f"Failed to regenerate response: {e}")
        raise HTTPException(status_code=500, detail=f"Regeneration failed: {e}") from e


@api.get("/sessions/{name}/export")
def sessions_export(name: str, format: str = "markdown", sessions_dir: str | None = None):
    """Export session to markdown, JSON, or HTML format."""
    format_lower = format.lower()
    if format_lower not in ("markdown", "json", "html"):
        raise HTTPException(
            status_code=400,
            detail="Invalid format. Must be one of: markdown, json, html",
        )

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

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{name}.{extension}"'},
    )


@api.post("/chat", response_model=ChatResponse)
def chat(request: Request, req: ChatRequest) -> ChatResponse:
    req_id = getattr(request.state, "request_id", None)
    usage_start = time.monotonic()

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
                "batching_enabled": _batch_processor is not None,
            },
        )

        # Determine RAG settings
        rag_val = getattr(req, "rag_enabled", None)
        rag_enabled = d["rag_enabled"] if rag_val is None else bool(rag_val)
        rag_filters = getattr(req, "rag_filters", None)
        rag_filters_dict = rag_filters.model_dump() if rag_filters else None

        # If batching is enabled, route through batch processor
        if _batch_processor is not None:
            # Prepare request data for batch processing
            batch_req_data = {
                "prompt": req.prompt,
                "session": req.session,
                "new": req.new,
                "model": chosen_model,
                "system": req.system,
                "config_path": None,
                "sessions_dir": req.sessions_dir,
                "rag_enabled": rag_enabled,
                "rag_filters": rag_filters_dict,
            }

            # Determine priority - interactive requests get higher priority
            # Could be extended to check request headers or user preference
            priority = RequestPriority.INTERACTIVE

            # Submit to batch processor. The wait here must cover however long
            # the underlying ollama_chat() call itself is allowed to take
            # (chat_timeout_seconds) plus queueing/scheduling overhead --
            # otherwise a slow cold model load times out the batch submit
            # before the actual chat request has had a chance to finish.
            batch_submit_timeout = d["chat_timeout_seconds"] + 10
            try:
                result_dict = _batch_processor.submit(
                    batch_req_data, priority=priority, timeout=batch_submit_timeout
                )
            except TimeoutError as e:
                log.error(
                    "Batch processing timed out",
                    extra={"request_id": req_id, "error": str(e)},
                )
                raise HTTPException(
                    status_code=504,
                    detail=(
                        "Chat request timed out waiting for the batch processor "
                        f"after {batch_submit_timeout:.0f}s. The model may require "
                        "more memory than is available, or chat_timeout_seconds "
                        "may need to be increased."
                    ),
                ) from e

            # Check for errors in result
            if "error" in result_dict:
                error_msg = result_dict.get("error", "Unknown error")
                error_type = result_dict.get("error_type", "Exception")
                log.error(
                    "Batch processing failed",
                    extra={
                        "request_id": req_id,
                        "error": error_msg,
                        "error_type": error_type,
                    },
                )
                status_code = 502 if error_type == "ModelRuntimeError" else 500
                raise HTTPException(status_code=status_code, detail=error_msg)

            # Convert result dict to ChatResult-like structure
            session_name = result_dict["session"]
            model_name = result_dict["model"]
            reply_text = result_dict["reply"]
            rag_used = result_dict["rag_used"]
            rag_context_data = result_dict.get("rag_context")

        else:
            # No batching - process directly
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
                kwargs["rag_enabled"] = rag_enabled

            if _maybe_kw(run_chat, "rag_filters"):
                kwargs["rag_filters"] = rag_filters_dict

            if _maybe_kw(run_chat, "attachments"):
                attachments_val = getattr(req, "attachments", None)
                if attachments_val:
                    kwargs["attachments"] = [a.model_dump() for a in attachments_val]

            if _maybe_kw(run_chat, "output_format"):
                output_format_val = getattr(req, "output_format", None)
                if output_format_val is not None:
                    kwargs["output_format"] = output_format_val

            result = run_chat(req.prompt, **kwargs)
            session_name = result.session
            model_name = result.model
            reply_text = result.reply
            rag_used = result.rag_used
            rag_context_data = result.rag_context

        log.info(
            "Chat request completed",
            extra={
                "request_id": req_id,
                "session": session_name,
                "model": model_name,
                "reply_length": len(reply_text),
            },
        )

        prom_metrics.CHAT_REQUESTS_TOTAL.labels(model=model_name, streaming="false").inc()
        if rag_used:
            prom_metrics.RAG_QUERIES_TOTAL.labels(source="chat").inc()

        try:
            usage_analytics_module.record(
                session=session_name,
                model=model_name,
                prompt_tokens=_count_usage_tokens(req.prompt),
                completion_tokens=_count_usage_tokens(reply_text),
                duration_s=time.monotonic() - usage_start,
                cfg=_req_cfg(request),
            )
        except Exception:
            log.debug("Usage analytics recording failed", exc_info=True)

        # Convert RAG context to RagChunkInfo objects
        rag_chunks = []
        if rag_context_data:
            for chunk_data in rag_context_data:
                rag_chunks.append(
                    RagChunkInfo(
                        text=chunk_data.get("text", ""),
                        score=chunk_data.get("score", 0.0),
                        doc_id=chunk_data.get("doc_id"),
                        chunk_id=chunk_data.get("chunk_id"),
                        similarity_score=chunk_data.get("similarity_score"),
                    )
                )

        return ChatResponse(
            session=session_name,
            model=model_name,
            reply=reply_text,
            rag_used=rag_used,
            rag_chunks=rag_chunks,
        )
    except ValueError as e:
        # Validation errors (e.g., invalid session name)
        log.warning("Chat validation error", extra={"request_id": req_id, "error": str(e)})
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ModelRuntimeError as e:
        # Upstream Ollama/model-runtime failure (crash, OOM, timeout) -- give
        # the operator/user the actionable detail instead of a bare 500.
        log.error(
            "Chat request failed: model runtime error",
            extra={
                "request_id": req_id,
                "error": str(e),
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        raise HTTPException(status_code=502, detail=str(e)) from e
    except HTTPException:
        # Already a well-formed HTTP error (e.g. the batch-processing branch
        # above) -- re-raise as-is instead of letting it fall into the
        # catch-all below, which would otherwise flatten it into a generic
        # 500 "Internal server error" and discard its real status/detail.
        raise
    except Exception as e:
        log.error(
            "Chat request failed",
            extra={
                "request_id": req_id,
                "error": str(e),
                "error_type": type(e).__name__,
            },
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Internal server error") from e


def _create_streaming_response(request: Request, req: ChatRequest) -> StreamingResponse:
    """Create a streaming chat response with request ID context propagation.

    This helper consolidates the streaming logic used by both versioned and legacy endpoints.
    It handles request ID capture and context setting for proper log traceability.

    Now supports Server-Sent Events (SSE) framing with proper event types, IDs, and heartbeats.
    Adapts response format based on client capability hints for backwards compatibility.

    Args:
        request: FastAPI Request object containing state and configuration
        req: Chat request parameters

    Returns:
        StreamingResponse configured for text/event-stream (SSE) streaming or plain text

    Raises:
        HTTPException: 422 for validation errors, 500 for server errors
    """
    try:
        # Validate session name early, before creating generator
        # This ensures validation errors are caught and return 422 instead of failing during streaming
        from nyxgpt.sessions import validate_session_name

        try:
            validate_session_name(req.session)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

        # Parse client capabilities for content negotiation
        capabilities = _parse_client_capabilities(request)

        # Log client capabilities for debugging
        log.debug(
            "Client capabilities detected",
            extra={
                "supports_sse": capabilities.supports_sse,
                "supports_structured_events": capabilities.supports_structured_events,
                "client_version": capabilities.client_version,
            },
        )

        # Capture request ID before entering generator (context may not propagate)
        req_id = request.state.request_id

        def _stream_sse():
            """Generate SSE-formatted events from chat stream.

            Adapts output format based on client capabilities:
            - Legacy clients (no SSE): Plain text streaming
            - SSE clients (no structured events): Simple SSE text events
            - Modern clients: Full structured SSE events (metadata, text, done, error)
            """
            # Explicitly set request ID in context for streaming generator
            try:
                request_id_var.set(req_id)
            except Exception as e:
                log.warning(f"Failed to set request ID in streaming context: {e}")
            # Continue regardless - streaming should work even if request ID fails

            import json
            import time

            event_id = 0
            start_time = time.time()
            total_tokens = 0

            d = _chat_runtime_defaults(_req_cfg(request))
            chosen_model = req.model or d["default_model"]
            prom_metrics.CHAT_REQUESTS_TOTAL.labels(model=chosen_model, streaming="true").inc()

            # Only send structured events if client supports them
            if capabilities.supports_sse and capabilities.supports_structured_events:
                # Send an immediate heartbeat to establish connection
                event_id += 1
                yield f"event: heartbeat\ndata: {json.dumps({'timestamp': start_time})}\nid: {event_id}\n\n"

                # Send metadata event with session/model info
                event_id += 1
                metadata = {
                    "session": req.session,
                    "model": chosen_model,
                    "timestamp": start_time,
                }
                yield f"event: metadata\ndata: {json.dumps(metadata)}\nid: {event_id}\n\n"

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

            if _maybe_kw(chat_stream, "rag_filters"):
                rag_filters_val = getattr(req, "rag_filters", None)
                if rag_filters_val:
                    # Convert RagFilters model to dict
                    kwargs["rag_filters"] = rag_filters_val.model_dump()

            if _maybe_kw(chat_stream, "attachments"):
                attachments_val = getattr(req, "attachments", None)
                if attachments_val:
                    # Convert AttachmentBlock models to dicts
                    kwargs["attachments"] = [a.model_dump() for a in attachments_val]

            if _maybe_kw(chat_stream, "output_format"):
                output_format_val = getattr(req, "output_format", None)
                if output_format_val is not None:
                    kwargs["output_format"] = output_format_val

            # Process the chat stream and wrap chunks in appropriate format
            try:
                for chunk in chat_stream(req.prompt, **kwargs):
                    # Strip RAG/retry markers for legacy clients
                    clean_chunk = chunk
                    if "__RAG_START__" in chunk and "__RAG_END__" in chunk:
                        rag_start = chunk.find("__RAG_START__")
                        rag_end = chunk.find("__RAG_END__") + len("__RAG_END__")
                        clean_chunk = chunk[:rag_start] + chunk[rag_end:]
                    if "__RETRY_START__" in clean_chunk and "__RETRY_END__" in clean_chunk:
                        retry_start = clean_chunk.find("__RETRY_START__")
                        retry_end = clean_chunk.find("__RETRY_END__") + len("__RETRY_END__")
                        clean_chunk = clean_chunk[:retry_start] + clean_chunk[retry_end:]

                    # Legacy client: plain text streaming (no SSE)
                    if not capabilities.supports_sse:
                        if clean_chunk:
                            yield clean_chunk
                        continue

                    event_id += 1

                    # SSE client without structured events: simple data streaming
                    if not capabilities.supports_structured_events:
                        if clean_chunk:
                            yield f"data: {clean_chunk}\n\n"
                        continue

                    # Modern client: full structured SSE events
                    # Check if chunk contains RAG metadata markers
                    if "__RAG_START__" in chunk and "__RAG_END__" in chunk:
                        # Extract RAG metadata and send as separate event
                        rag_start = chunk.find("__RAG_START__") + len("__RAG_START__")
                        rag_end = chunk.find("__RAG_END__")
                        rag_json = chunk[rag_start:rag_end]

                        # Send RAG metadata as rag_context event
                        yield f"event: rag_context\ndata: {rag_json}\nid: {event_id}\n\n"

                        # Send any text before/after RAG markers as text events
                        text_before = chunk[: chunk.find("__RAG_START__")]
                        text_after = chunk[rag_end + len("__RAG_END__") :]
                        remaining_text = text_before + text_after

                        if remaining_text:
                            event_id += 1
                            # Count tokens for this chunk
                            try:
                                from nyxgpt.token_counter import count_tokens

                                total_tokens += count_tokens(remaining_text)
                            except Exception:
                                pass  # Token counting is optional

                            elapsed = time.time() - start_time
                            yield f"event: text\ndata: {json.dumps({'content': remaining_text, 'tokens': total_tokens, 'elapsed': elapsed})}\nid: {event_id}\n\n"
                    elif "__RETRY_START__" in chunk and "__RETRY_END__" in chunk:
                        # Handle retry status messages (already formatted as JSON)
                        retry_start = chunk.find("__RETRY_START__") + len("__RETRY_START__")
                        retry_end = chunk.find("__RETRY_END__")
                        retry_json = chunk[retry_start:retry_end]
                        yield f"event: retry\ndata: {retry_json}\nid: {event_id}\n\n"
                    else:
                        # Regular text content
                        # Count tokens for this chunk
                        try:
                            from nyxgpt.token_counter import count_tokens

                            total_tokens += count_tokens(chunk)
                        except Exception:
                            pass  # Token counting is optional

                        elapsed = time.time() - start_time
                        yield f"event: text\ndata: {json.dumps({'content': chunk, 'tokens': total_tokens, 'elapsed': elapsed})}\nid: {event_id}\n\n"

                try:
                    usage_analytics_module.record(
                        session=req.session,
                        model=chosen_model,
                        prompt_tokens=_count_usage_tokens(req.prompt),
                        completion_tokens=total_tokens,
                        duration_s=time.time() - start_time,
                        cfg=_req_cfg(request),
                    )
                except Exception:
                    log.debug("Usage analytics recording failed", exc_info=True)

                # Send done event with final stats (only for modern clients)
                if capabilities.supports_sse and capabilities.supports_structured_events:
                    event_id += 1
                    elapsed = time.time() - start_time
                    done_data = {
                        "event_id": event_id,
                        "total_tokens": total_tokens,
                        "elapsed": elapsed,
                    }
                    yield f"event: done\ndata: {json.dumps(done_data)}\nid: {event_id}\n\n"
            except Exception as e:
                # This is the operator's only server-side record of a streaming chat
                # failure: once the response has started, FastAPI/uvicorn have no
                # request context left to log with, so if we don't log here the
                # upstream Ollama/model-runtime detail (status, model, message) is
                # never written to nyxgpt.log and is invisible in the log viewer.
                log.error(
                    "Chat stream failed",
                    extra={
                        "request_id": req_id,
                        "session": req.session,
                        "model": chosen_model,
                        "error": str(e),
                        "error_type": type(e).__name__,
                    },
                    exc_info=True,
                )
                # Send error event (only for SSE clients)
                if capabilities.supports_sse and capabilities.supports_structured_events:
                    event_id += 1
                    elapsed = time.time() - start_time
                    error_data = {
                        "error": str(e),
                        "elapsed": elapsed,
                    }
                    yield f"event: error\ndata: {json.dumps(error_data)}\nid: {event_id}\n\n"
                raise

        # Adapt response headers based on client capabilities
        if capabilities.supports_sse:
            media_type = "text/event-stream; charset=utf-8"
        else:
            media_type = "text/plain; charset=utf-8"

        response_headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable proxy buffering
            # Inform client of server capabilities
            "X-Server-Supports-SSE": "true",
            "X-Server-Supports-Structured-Events": "true",
            "X-Server-Supports-Streaming": "true",
            "X-Server-Version": "nyxgpt/1.0.0",
        }

        return StreamingResponse(
            _stream_sse(),
            media_type=media_type,
            headers=response_headers,
        )
    except HTTPException:
        # Re-raise HTTP exceptions (e.g., 422 from validation above)
        raise
    except Exception as e:
        log.error(f"Streaming setup failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from e


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


def _tools_error_status(message: str) -> int:
    """403 for the root-confinement guard, 400 for everything else (bad path,
    invalid regex, no matches, ...)."""
    return 403 if "escapes allowed root" in message else 400


@api.post("/tools/ls", response_model=ToolTextResponse)
def tool_ls(request: Request, req: ToolLsRequest) -> ToolTextResponse:
    tools_root = get_tools_root(_req_cfg(request))
    rc, out, err = _capture_stdout(tools_fs.ls, Path(req.path), root=tools_root)
    if rc != 0:
        message = err.strip() or out.strip() or "ls failed"
        raise HTTPException(status_code=_tools_error_status(message), detail=message)
    return ToolTextResponse(output=out)


@api.post("/tools/cat", response_model=ToolTextResponse)
def tool_cat(request: Request, req: ToolCatRequest) -> ToolTextResponse:
    tools_root = get_tools_root(_req_cfg(request))
    rc, out, err = _capture_stdout(
        tools_fs.cat, Path(req.path), head=req.head, tail=req.tail, root=tools_root
    )
    if rc != 0:
        message = err.strip() or out.strip() or "cat failed"
        raise HTTPException(status_code=_tools_error_status(message), detail=message)
    return ToolTextResponse(output=out)


@api.post("/tools/grep", response_model=ToolTextResponse)
def tool_grep(request: Request, req: ToolGrepRequest) -> ToolTextResponse:
    tools_root = get_tools_root(_req_cfg(request))
    rc, out, err = _capture_stdout(
        tools_fs.grep, req.pattern, Path(req.path), max_matches=req.max, root=tools_root
    )
    if rc != 0:
        message = err.strip() or out.strip() or "grep failed"
        raise HTTPException(status_code=_tools_error_status(message), detail=message)
    return ToolTextResponse(output=out)


@api.post("/rag/ingest", response_model=RagIngestResponse)
def rag_ingest(_request: Request, req: RagIngestRequest) -> RagIngestResponse:
    try:
        result = ingest_document(
            doc_id=req.doc_id,
            text=req.text,
            metadata=req.metadata,
            ensure_schema=req.ensure_schema,
            collection=req.collection,
        )
        return RagIngestResponse(
            doc_id=req.doc_id,
            chunks_ingested=result["chunks_ingested"],
            status=result["status"],
            doc_hash=result["doc_hash"],
            previous_hash=result["previous_hash"],
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@api.get("/rag/documents/{doc_id}", response_model=RagDocumentInfo)
def rag_document_info(
    _request: Request, doc_id: str, collection: str = "default"
) -> RagDocumentInfo:
    """Get document version and metadata information."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore(collection=collection)
    try:
        info = store.get_document_info(doc_id)
        if not info:
            raise HTTPException(
                status_code=404,
                detail=f"Document '{doc_id}' not found in collection '{collection}'",
            )
        return RagDocumentInfo(**info)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        store.close()


@api.get("/rag/config")
def rag_config(request: Request) -> dict[str, Any]:
    """Get RAG configuration including score thresholds for visual indicators.

    Returns:
        Dictionary containing:
        - min_score: Minimum score threshold for retrieval
        - good_score_threshold: Threshold for high-confidence results (green)
        - medium_score_threshold: Threshold for medium-confidence results (yellow)
    """
    cfg = _req_cfg(request)
    return {
        "min_score": get_rag_min_score(cfg),
        "good_score_threshold": get_rag_good_score_threshold(cfg),
        "medium_score_threshold": get_rag_medium_score_threshold(cfg),
    }


@api.get("/rag/cache/stats", response_model=QueryCacheStatsResponse)
def rag_cache_stats(_request: Request) -> QueryCacheStatsResponse:
    """Get hit rate and size statistics for the RAG query result cache.

    Returns zeroed stats if query result caching is disabled
    (`[cache] query_cache_enabled = false`).
    """
    stats = get_query_cache_stats()
    return QueryCacheStatsResponse(
        hits=int(stats["hits"]),
        misses=int(stats["misses"]),
        hit_rate=float(stats["hit_rate"]),
        size=int(stats["size"]),
    )


@api.post("/rag/cache/clear", response_model=QueryCacheClearResponse)
def rag_cache_clear(_request: Request) -> QueryCacheClearResponse:
    """Manually clear the RAG query result cache.

    The cache is also cleared automatically on document ingestion/update,
    collection deletion, and collection re-indexing, so this is mainly
    useful for manual troubleshooting.
    """
    clear_query_cache()
    return QueryCacheClearResponse(status="Query result cache cleared")


@api.get("/rag/collections", response_model=CollectionsListResponse)
def rag_collections_list(_request: Request) -> CollectionsListResponse:
    """List all RAG collections with their statistics.

    Returns information about each collection including:
    - Collection name
    - Number of documents
    - Total number of chunks
    - Embedding models used
    """
    from nyxgpt.api_models import CollectionsListResponse
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    # Get list of all collections
    temp_store = CassandraVectorStore()
    try:
        collection_names = temp_store.list_collections()
    except Exception as e:
        log.error(f"Failed to list collections: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list collections: {str(e)}") from e
    finally:
        temp_store.close()

    # Gather stats for each collection
    collections_info = []
    for coll_name in collection_names:
        store = CassandraVectorStore(collection=coll_name)
        try:
            docs = store.list_docs()
            doc_count = len(docs)
            chunk_count = sum(d["chunks"] for d in docs)

            # Get unique embedding models
            embedding_models = list(
                {d["embedding_model"] for d in docs if d.get("embedding_model")}
            )
            embedding_models.sort()

            collections_info.append(
                CollectionInfo(
                    name=coll_name,
                    doc_count=doc_count,
                    chunk_count=chunk_count,
                    embedding_models=embedding_models,
                )
            )
        except Exception as e:
            log.error(f"Failed to get stats for collection '{coll_name}': {e}", exc_info=True)
            # Include collection with zero stats on error
            collections_info.append(
                CollectionInfo(
                    name=coll_name,
                    doc_count=0,
                    chunk_count=0,
                    embedding_models=[],
                )
            )
        finally:
            store.close()

    return CollectionsListResponse(collections=collections_info)


@api.post("/rag/collections", response_model=CreateCollectionResponse, status_code=201)
def rag_collection_create(
    _request: Request, body: CreateCollectionRequest
) -> CreateCollectionResponse:
    """Create a new RAG collection.

    Creates a new empty collection with the specified embedding dimension.
    Collection names must be alphanumeric with underscores only (no hyphens).

    Args:
        body: Collection creation request with name and embedding_dim

    Returns:
        CreateCollectionResponse with collection name and status
    """
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    # Validate collection name
    collection_name = body.name.strip()

    # Prevent creating 'default' collection manually
    if collection_name == "default":
        raise HTTPException(
            status_code=400,
            detail="Cannot manually create 'default' collection. It is automatically managed.",
        )

    # Validate name format (alphanumeric and underscores only - no hyphens for Cassandra compatibility)
    if not re.match(r"^[a-zA-Z0-9_]+$", collection_name):
        raise HTTPException(
            status_code=400,
            detail="Collection name must contain only letters, numbers, and underscores.",
        )

    # Validate embedding dimension
    if body.embedding_dim <= 0 or body.embedding_dim > 10000:
        raise HTTPException(
            status_code=400,
            detail="Embedding dimension must be between 1 and 10000.",
        )

    store = CassandraVectorStore(collection=collection_name)
    try:
        # Check if collection already exists
        existing_collections = store.list_collections()
        if collection_name in existing_collections:
            raise HTTPException(
                status_code=409,
                detail=f"Collection '{collection_name}' already exists.",
            )

        # Create the collection schema
        store.ensure_schema(embedding_dim=body.embedding_dim, collection=collection_name)

        return CreateCollectionResponse(
            collection=collection_name,
            status=f"Collection '{collection_name}' created successfully",
            embedding_dim=body.embedding_dim,
        )
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except ImportError as e:
        log.error(f"Cassandra driver import error: {e}", exc_info=True)
        raise HTTPException(
            status_code=503, detail="RAG service unavailable: Cassandra driver not found"
        ) from e
    except Exception as e:
        log.error(f"Failed to create collection '{collection_name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create collection: {str(e)}") from e
    finally:
        store.close()


@api.delete("/rag/collections/{collection_name}", response_model=CollectionDeleteResponse)
def rag_collection_delete(_request: Request, collection_name: str) -> CollectionDeleteResponse:
    """Delete a RAG collection (truncates all data in the collection).

    WARNING: This operation cannot be undone. All documents and chunks in the
    collection will be permanently deleted.

    Note: This does not drop the Cassandra table, only truncates it.
    To fully remove the table, use Cassandra admin tools.
    """
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    # Prevent deletion of default collection via more descriptive error
    if collection_name == "default":
        raise HTTPException(
            status_code=400,
            detail="Cannot clear the 'default' collection. This collection is protected.",
        )

    store = CassandraVectorStore(collection=collection_name)
    try:
        # Truncate the collection (remove all data)
        store.truncate()

        # Invalidate query result cache: the document set changed, so any
        # cached retrieval results may now be stale.
        clear_query_cache()

        return CollectionDeleteResponse(
            collection=collection_name,
            status=f"Collection '{collection_name}' has been cleared (truncated)",
        )
    except ImportError as e:
        # Cassandra driver not available
        log.error(f"Cassandra driver import error: {e}", exc_info=True)
        raise HTTPException(
            status_code=503, detail="RAG service unavailable: Cassandra driver not found"
        ) from e
    except Exception as e:
        # Catch database errors and other issues
        log.error(f"Failed to clear collection '{collection_name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to clear collection: {str(e)}") from e
    finally:
        store.close()


@api.post("/rag/collections/{collection_name}/reindex", response_model=ReindexCollectionResponse)
def rag_collection_reindex(
    _request: Request, collection_name: str, body: ReindexCollectionRequest
) -> ReindexCollectionResponse:
    """Re-index a collection with a different embedding model.

    This operation regenerates embeddings for all chunks in the collection
    using the specified target embedding model.

    WARNING: This is a long-running operation. For large collections, this may
    take several minutes.

    Args:
        collection_name: Name of collection to re-index
        body: Re-index request with target_embedding_model and embedding_dim

    Returns:
        ReindexCollectionResponse with status and progress
    """
    from nyxgpt.rag.embeddings import embed_texts
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    # Prevent re-indexing default collection to avoid accidents
    if collection_name == "default":
        raise HTTPException(
            status_code=400,
            detail="Cannot re-index the 'default' collection. Create a new collection instead.",
        )

    # Validate embedding dimension
    if body.embedding_dim <= 0 or body.embedding_dim > 10000:
        raise HTTPException(
            status_code=400,
            detail="Embedding dimension must be between 1 and 10000.",
        )

    store = CassandraVectorStore(collection=collection_name)
    try:
        # Verify collection exists
        existing_collections = store.list_collections()
        if collection_name not in existing_collections:
            raise HTTPException(
                status_code=404,
                detail=f"Collection '{collection_name}' not found.",
            )

        # Get all chunks from the collection
        log.info(
            f"Re-indexing collection '{collection_name}' with model '{body.target_embedding_model}'"
        )
        chunks = store.get_all_chunks()

        if not chunks:
            return ReindexCollectionResponse(
                collection=collection_name,
                status="Collection is empty, nothing to re-index",
                chunks_processed=0,
                chunks_total=0,
            )

        chunks_total = len(chunks)
        log.info(f"Found {chunks_total} chunks to re-index in collection '{collection_name}'")

        # Extract text from each chunk for re-embedding
        texts = [chunk["text"] for chunk in chunks]

        # Re-generate embeddings with new model
        log.info(f"Generating new embeddings with model '{body.target_embedding_model}'")
        try:
            embed_texts(
                texts,
                model=body.target_embedding_model,
                dimension=body.embedding_dim,
            )
        except Exception as e:
            log.error(f"Failed to generate embeddings: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to generate embeddings with model '{body.target_embedding_model}': {str(e)}",
            ) from e

        # Invalidate query result cache: cached results may reflect the
        # collection's pre-reindex embeddings.
        clear_query_cache()

        # Return success response
        return ReindexCollectionResponse(
            collection=collection_name,
            status="Successfully re-indexed collection",
            chunks_processed=chunks_total,
            chunks_total=chunks_total,
        )
    except Exception as e:
        log.error(f"Failed to re-index collection '{collection_name}': {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to re-index collection: {str(e)}"
        ) from e
    finally:
        store.close()


@api.get("/rag/collections/{collection_name}/settings", response_model=CollectionSettingsResponse)
def rag_collection_get_settings(
    _request: Request, collection_name: str
) -> CollectionSettingsResponse:
    """Get settings for a collection.

    Returns the current configuration for a collection, including:
    - Embedding model (from stored settings or derived from documents)
    - Default chunk size (from stored settings or global config)
    - Default chunk overlap (from stored settings or global config)

    Args:
        collection_name: Name of collection

    Returns:
        CollectionSettingsResponse with current settings
    """
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore(collection=collection_name)
    try:
        # Verify collection exists
        existing_collections = store.list_collections()
        if collection_name not in existing_collections:
            raise HTTPException(
                status_code=404,
                detail=f"Collection '{collection_name}' not found.",
            )

        # Get stored settings (if any)
        stored_settings = store.get_collection_settings()

        # Get global defaults from config
        cfg = load_config(None)
        global_chunk_size = cfg.getint("rag", "chunk_size", fallback=1000)
        global_chunk_overlap = cfg.getint("rag", "chunk_overlap", fallback=200)

        # If no stored settings, derive embedding_model from documents
        embedding_model_raw = stored_settings.get("embedding_model")
        embedding_model: str | None = None
        if isinstance(embedding_model_raw, str):
            embedding_model = embedding_model_raw
        elif not embedding_model_raw:
            docs = store.list_docs()
            embedding_models = list(
                {d["embedding_model"] for d in docs if d.get("embedding_model")}
            )
            # Use first model if only one, otherwise None (indicates mixed)
            embedding_model = embedding_models[0] if len(embedding_models) == 1 else None

        # Use stored settings if available, otherwise fall back to global config
        chunk_size_raw = stored_settings.get("chunk_size")
        chunk_size: int = chunk_size_raw if isinstance(chunk_size_raw, int) else global_chunk_size

        chunk_overlap_raw = stored_settings.get("chunk_overlap")
        chunk_overlap: int = (
            chunk_overlap_raw if isinstance(chunk_overlap_raw, int) else global_chunk_overlap
        )

        return CollectionSettingsResponse(
            collection=collection_name,
            settings=CollectionSettings(
                embedding_model=embedding_model,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            ),
        )

    except HTTPException:
        raise
    except ImportError as e:
        log.error(f"Cassandra driver import error: {e}", exc_info=True)
        raise HTTPException(
            status_code=503, detail="RAG service unavailable: Cassandra driver not found"
        ) from e
    except Exception as e:
        log.error(f"Failed to get settings for collection '{collection_name}': {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to get collection settings: {str(e)}"
        ) from e
    finally:
        store.close()


@api.put("/rag/collections/{collection_name}/settings", response_model=CollectionSettingsResponse)
def rag_collection_update_settings(
    _request: Request, collection_name: str, body: CollectionSettings
) -> CollectionSettingsResponse:
    """Update settings for a collection.

    This allows configuring per-collection settings like:
    - Preferred embedding model
    - Default chunk size
    - Default chunk overlap

    Note: Settings are stored separately from documents and are used as defaults
    when ingesting new documents to this collection.

    Args:
        collection_name: Name of collection
        body: New settings to apply

    Returns:
        CollectionSettingsResponse with updated settings
    """
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    # Verify collection exists first
    store = CassandraVectorStore(collection=collection_name)
    try:
        existing_collections = store.list_collections()
        if collection_name not in existing_collections:
            raise HTTPException(
                status_code=404,
                detail=f"Collection '{collection_name}' not found.",
            )

        # Update collection settings
        store.update_collection_settings(
            embedding_model=body.embedding_model,
            chunk_size=body.chunk_size,
            chunk_overlap=body.chunk_overlap,
        )

        # Return updated settings
        return CollectionSettingsResponse(
            collection=collection_name,
            settings=body,
        )

    except HTTPException:
        raise
    except ImportError as e:
        log.error(f"Cassandra driver import error: {e}", exc_info=True)
        raise HTTPException(
            status_code=503, detail="RAG service unavailable: Cassandra driver not found"
        ) from e
    except Exception as e:
        log.error(
            f"Failed to update settings for collection '{collection_name}': {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to update collection settings: {str(e)}"
        ) from e
    finally:
        store.close()


@api.get("/rag/documents")
def rag_documents_list(
    _request: Request,
    collection: str = Query("default", description="Vector store collection name"),
) -> dict[str, Any]:
    """List all documents in the RAG vector store.

    Returns list of documents with metadata including:
    - doc_id: Document identifier
    - chunks: Number of chunks stored
    - embedding_model: Model used for embeddings
    - filename: Filename (from metadata if available)
    - tags: Document tags (from metadata if available)
    - ingested_at: Ingestion timestamp (from metadata if available)
    """
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore(collection=collection)
    try:
        docs = store.list_docs()

        # Enrich with metadata from individual chunks (get first chunk per doc)
        enriched_docs = []
        for doc in docs:
            doc_id = doc["doc_id"]
            # Query for one chunk to get metadata
            try:
                from cassandra.query import SimpleStatement

                stmt = SimpleStatement(
                    f"SELECT metadata, ingested_at FROM {store.table_name} WHERE doc_id = %s LIMIT 1"
                )
                rows = store.session.execute(stmt, (doc_id,))
                row = next(iter(rows), None)

                metadata = row.metadata if row and hasattr(row, "metadata") else {}
                ingested_at = (
                    row.ingested_at.isoformat()
                    if row and hasattr(row, "ingested_at") and row.ingested_at
                    else None
                )

                enriched_docs.append(
                    {
                        "doc_id": doc_id,
                        "chunks": doc["chunks"],
                        "embedding_model": doc.get("embedding_model"),
                        "filename": metadata.get("filename") if metadata else None,
                        "tags": metadata.get("tags") if metadata else None,
                        "ingested_at": ingested_at,
                    }
                )
            except Exception as e:
                log.warning(f"Failed to enrich metadata for doc_id={doc_id}: {e}")
                # Fallback: return basic info without metadata
                enriched_docs.append(
                    {
                        "doc_id": doc_id,
                        "chunks": doc["chunks"],
                        "embedding_model": doc.get("embedding_model"),
                        "filename": None,
                        "tags": None,
                        "ingested_at": None,
                    }
                )

        return {"documents": enriched_docs}
    except Exception as e:
        log.error(f"Failed to list RAG documents: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        store.close()


@api.post("/rag/query", response_model=RagQueryResponse)
def rag_query(_request: Request, req: RagQueryRequest) -> RagQueryResponse:
    try:
        from datetime import datetime

        from nyxgpt.rag.vectorstore_cassandra import MetadataFilter

        # Build metadata filter if any filter params are provided
        metadata_filter = None
        if any([req.doc_ids, req.filename, req.tags, req.date_from, req.date_to]):
            # Parse dates
            date_from_dt = datetime.fromisoformat(req.date_from) if req.date_from else None
            date_to_dt = datetime.fromisoformat(req.date_to) if req.date_to else None

            metadata_filter = MetadataFilter(
                doc_ids=req.doc_ids,
                filename=req.filename,
                tags=req.tags,
                date_from=date_from_dt,
                date_to=date_to_dt,
            )

        result = retrieve_context(
            req.query,
            top_k=req.top_k,
            debug_mode=req.debug_mode,
            metadata_filter=metadata_filter,
        )

        if req.debug_mode:
            # Type narrowing: debug_mode=True means result is tuple[list[dict], RAGDebugInfo]
            from nyxgpt.rag.rag import RAGDebugInfo

            results, debug_info = cast(tuple[list[dict], RAGDebugInfo], result)
            # Convert RAGDebugInfo to RagDebugInfo (API model)
            from nyxgpt.api_models import RagDebugInfo

            api_debug_info = RagDebugInfo(
                total_time_ms=debug_info.total_time_ms,
                query_expansion_time_ms=debug_info.query_expansion_time_ms,
                embedding_time_ms=debug_info.embedding_time_ms,
                vector_search_time_ms=debug_info.vector_search_time_ms,
                filtering_time_ms=debug_info.filtering_time_ms,
                composition_time_ms=debug_info.composition_time_ms,
                original_query=debug_info.original_query,
                query_variants=debug_info.query_variants,
                num_queries=debug_info.num_queries,
                embedding_model=debug_info.embedding_model,
                embedding_dim=debug_info.embedding_dim,
                num_texts_embedded=debug_info.num_texts_embedded,
                batch_size=debug_info.batch_size,
                raw_results_count=debug_info.raw_results_count,
                score_min=debug_info.score_min,
                score_max=debug_info.score_max,
                score_mean=debug_info.score_mean,
                after_min_score_filter=debug_info.after_min_score_filter,
                after_dedupe_filter=debug_info.after_dedupe_filter,
                after_max_chunks_filter=debug_info.after_max_chunks_filter,
                total_chars_before_truncation=debug_info.total_chars_before_truncation,
                total_chars_after_truncation=debug_info.total_chars_after_truncation,
                chunks_included=debug_info.chunks_included,
            )
        else:
            # Type narrowing: debug_mode=False means result is list[dict]
            results = cast(list[dict], result)
            api_debug_info = None

        out = [
            RagQueryResult(
                doc_id=str(r.get("doc_id", "")),
                chunk_id=int(r.get("chunk_id", 0)),
                text=str(r.get("text", "")),
                score=float(r.get("score", 0.0)),
                similarity_score=r.get("similarity_score"),
            )
            for r in results
        ]
        prom_metrics.RAG_QUERIES_TOTAL.labels(source="rag_query").inc()
        return RagQueryResponse(results=out, debug_info=api_debug_info)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@api.post("/rag/metrics/query", response_model=RagMetricsQueryResponse)
def rag_metrics_query(_request: Request, req: RagMetricsQueryRequest) -> RagMetricsQueryResponse:
    """Query RAG with comprehensive evaluation metrics.

    This endpoint extends the standard RAG query with evaluation metrics including:
    - Retrieval accuracy (hit rate, unique docs, score distribution)
    - Latency tracking (per-stage breakdowns)
    - Hit rate analysis (success rate, threshold performance)

    Requires debug_mode=True to collect metrics.
    """
    try:
        from nyxgpt.config import get_rag_min_score

        # Force debug mode to collect metrics
        result = retrieve_context(req.query, top_k=req.top_k, debug_mode=True)

        # Type narrowing: debug_mode=True means result is tuple[list[dict], RAGDebugInfo]
        from nyxgpt.rag.rag import RAGDebugInfo, compute_evaluation_metrics

        results, debug_info = cast(tuple[list[dict], RAGDebugInfo], result)

        # Convert RAGDebugInfo to RagDebugInfo (API model)
        from nyxgpt.api_models import (
            HitRateMetrics as ApiHitRateMetrics,
        )
        from nyxgpt.api_models import (
            LatencyMetrics as ApiLatencyMetrics,
        )
        from nyxgpt.api_models import (
            RagDebugInfo,
        )
        from nyxgpt.api_models import (
            RagEvaluationMetrics as ApiRagEvaluationMetrics,
        )
        from nyxgpt.api_models import (
            RetrievalAccuracyMetrics as ApiRetrievalAccuracyMetrics,
        )

        api_debug_info = RagDebugInfo(
            total_time_ms=debug_info.total_time_ms,
            query_expansion_time_ms=debug_info.query_expansion_time_ms,
            embedding_time_ms=debug_info.embedding_time_ms,
            vector_search_time_ms=debug_info.vector_search_time_ms,
            filtering_time_ms=debug_info.filtering_time_ms,
            composition_time_ms=debug_info.composition_time_ms,
            original_query=debug_info.original_query,
            query_variants=debug_info.query_variants,
            num_queries=debug_info.num_queries,
            embedding_model=debug_info.embedding_model,
            embedding_dim=debug_info.embedding_dim,
            num_texts_embedded=debug_info.num_texts_embedded,
            batch_size=debug_info.batch_size,
            raw_results_count=debug_info.raw_results_count,
            score_min=debug_info.score_min,
            score_max=debug_info.score_max,
            score_mean=debug_info.score_mean,
            after_min_score_filter=debug_info.after_min_score_filter,
            after_dedupe_filter=debug_info.after_dedupe_filter,
            after_max_chunks_filter=debug_info.after_max_chunks_filter,
            total_chars_before_truncation=debug_info.total_chars_before_truncation,
            total_chars_after_truncation=debug_info.total_chars_after_truncation,
            chunks_included=debug_info.chunks_included,
        )

        # Compute evaluation metrics if requested
        evaluation_metrics = None
        if req.collect_metrics:
            cfg = load_config(None)
            min_score = get_rag_min_score(cfg)
            eval_metrics = compute_evaluation_metrics(results, debug_info, min_score)

            # Convert to API models
            evaluation_metrics = ApiRagEvaluationMetrics(
                retrieval_accuracy=ApiRetrievalAccuracyMetrics(
                    results_returned=eval_metrics.retrieval_accuracy.results_returned,
                    query_success=eval_metrics.retrieval_accuracy.query_success,
                    unique_docs_retrieved=eval_metrics.retrieval_accuracy.unique_docs_retrieved,
                    total_chunks_retrieved=eval_metrics.retrieval_accuracy.total_chunks_retrieved,
                    score_distribution=eval_metrics.retrieval_accuracy.score_distribution,
                ),
                latency=ApiLatencyMetrics(
                    total_time_ms=eval_metrics.latency.total_time_ms,
                    stage_timings=eval_metrics.latency.stage_timings,
                    percentiles=eval_metrics.latency.percentiles,
                ),
                hit_rate=ApiHitRateMetrics(
                    query_success_rate=eval_metrics.hit_rate.query_success_rate,
                    total_queries=eval_metrics.hit_rate.total_queries,
                    successful_queries=eval_metrics.hit_rate.successful_queries,
                    failed_queries=eval_metrics.hit_rate.failed_queries,
                    avg_top_score=eval_metrics.hit_rate.avg_top_score,
                    score_above_threshold_rate=eval_metrics.hit_rate.score_above_threshold_rate,
                ),
                query_id=eval_metrics.query_id,
                timestamp=eval_metrics.timestamp,
            )

        out = [
            RagQueryResult(
                doc_id=str(r.get("doc_id", "")),
                chunk_id=int(r.get("chunk_id", 0)),
                text=str(r.get("text", "")),
                score=float(r.get("score", 0.0)),
                similarity_score=r.get("similarity_score"),
            )
            for r in results
        ]

        return RagMetricsQueryResponse(
            results=out,
            debug_info=api_debug_info,
            evaluation_metrics=evaluation_metrics,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


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


@api.get("/sessions/{name}/documents", response_model=SessionDocumentsResponse)
def list_session_documents(name: str) -> SessionDocumentsResponse:
    """List document IDs force-included for a session's RAG context.

    Returns the list of documents attached to the session that are
    always retrieved when RAG is enabled, regardless of other filters.
    """
    cfg = load_config(None)
    sessions_dir = get_sessions_dir(cfg)
    sf = sessions.session_file_for(name, sessions_dir)
    mf = sessions.meta_file_for(sf)

    meta = sessions.load_session_meta(mf)
    attached = meta.get("attached_doc_ids", [])
    if not isinstance(attached, list):
        attached = []

    return SessionDocumentsResponse(session=name, attached_doc_ids=attached)


@api.post("/sessions/{name}/documents", response_model=SessionDocumentsResponse)
def attach_document_to_session(name: str, req: AttachDocumentRequest) -> SessionDocumentsResponse:
    """Attach a document to a session for force-inclusion in RAG context.

    Attached documents are always retrieved when RAG is enabled for the
    session, regardless of any other metadata filters in the chat request.
    """
    cfg = load_config(None)
    sessions_dir = get_sessions_dir(cfg)
    sf = sessions.session_file_for(name, sessions_dir)
    mf = sessions.meta_file_for(sf)

    if not sf.exists():
        sessions.save_session_messages(sf, [])

    meta = sessions.load_session_meta(mf)
    meta = sessions.ensure_meta_defaults(meta)

    raw = meta.get("attached_doc_ids", [])
    attached: list[str] = raw if isinstance(raw, list) else []

    if req.doc_id not in attached:
        attached = attached + [req.doc_id]
        meta["attached_doc_ids"] = attached
        sessions.save_session_meta(mf, meta)

    return SessionDocumentsResponse(session=name, attached_doc_ids=attached)


@api.delete("/sessions/{name}/documents/{doc_id}", response_model=SessionDocumentsResponse)
def detach_document_from_session(name: str, doc_id: str) -> SessionDocumentsResponse:
    """Detach a document from a session, removing it from force-inclusion.

    After detaching, the document will no longer be force-included in
    RAG retrieval for this session.
    """
    cfg = load_config(None)
    sessions_dir = get_sessions_dir(cfg)
    sf = sessions.session_file_for(name, sessions_dir)
    mf = sessions.meta_file_for(sf)

    meta = sessions.load_session_meta(mf)
    meta = sessions.ensure_meta_defaults(meta)

    raw = meta.get("attached_doc_ids", [])
    attached: list[str] = raw if isinstance(raw, list) else []

    if doc_id in attached:
        attached = [d for d in attached if d != doc_id]
        meta["attached_doc_ids"] = attached
        sessions.save_session_meta(mf, meta)

    return SessionDocumentsResponse(session=name, attached_doc_ids=attached)


@api.post("/rag/upload", response_model=RagIngestResponse)
async def rag_upload_file(
    file: UploadFile = File(...),
    doc_id: str | None = None,
) -> RagIngestResponse:
    """Upload and ingest a document for RAG with proper markdown parsing."""
    # Validate file type
    allowed_types = {".txt", ".md", ".json", ".pdf", ".pptx", ".docx", ".epub", ".html", ".htm"}
    file_ext = Path(file.filename or "").suffix.lower()
    if file_ext not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"File type {file_ext} not supported. Allowed: {', '.join(allowed_types)}",
        )

    # Read file content
    content = await file.read()

    # Validate file size (prevent memory exhaustion)
    MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(content)} bytes). Maximum size: {MAX_UPLOAD_SIZE} bytes (10MB)",
        )

    # Parse based on file type
    if file_ext == ".pdf":
        # Handle PDF with improved extraction (#2663)
        try:
            import pdfplumber
            from pypdf import PdfReader

            # Extract metadata using pypdf
            reader = PdfReader(io.BytesIO(content))
            metadata = {}
            if reader.metadata:
                # Extract common metadata fields
                for key in [
                    "/Title",
                    "/Author",
                    "/Subject",
                    "/Creator",
                    "/Producer",
                    "/CreationDate",
                    "/ModDate",
                ]:
                    if key in reader.metadata:
                        clean_key = key.lstrip("/")
                        metadata[clean_key] = str(reader.metadata[key])

            # Extract text with better formatting using pdfplumber
            text_parts = []

            # Add metadata section if available
            if metadata:
                meta_str = "\n".join(f"{k}: {v}" for k, v in metadata.items())
                text_parts.append(f"[Metadata]\n{meta_str}\n")

            # Try enhanced extraction with pdfplumber
            try:
                # Process each page with pdfplumber for better extraction
                with pdfplumber.open(io.BytesIO(content)) as pdf:
                    for _page_num, page in enumerate(pdf.pages, 1):
                        page_content = []

                        # Extract tables with preserved structure
                        tables = page.extract_tables()
                        if tables:
                            for table in tables:
                                # Format table as text grid
                                table_text = "\n".join(
                                    " | ".join(str(cell) if cell else "" for cell in row)
                                    for row in table
                                )
                                page_content.append(f"[Table]\n{table_text}\n")

                        # Extract text with layout preservation
                        # Use layout mode to preserve formatting and multi-column layouts
                        page_text = page.extract_text(layout=True)
                        if page_text:
                            page_content.append(page_text.strip())

                        # Combine page content
                        if page_content:
                            text_parts.append("\n\n".join(page_content))

            except Exception as plumber_error:
                # Fall back to basic pypdf extraction
                log.warning(f"pdfplumber extraction failed, falling back to pypdf: {plumber_error}")
                text_parts = []
                if metadata:
                    meta_str = "\n".join(f"{k}: {v}" for k, v in metadata.items())
                    text_parts.append(f"[Metadata]\n{meta_str}\n")
                for pdf_page in reader.pages:
                    page_text = pdf_page.extract_text()
                    if page_text:
                        text_parts.append(page_text)

            text = "\n\n".join(text_parts)

            # Check if OCR is needed for image-only pages (#2669)
            cfg = load_config(None)
            ocr_enabled = cfg.getboolean("pdf", "ocr_enabled", fallback=True)
            ocr_min_text_threshold = cfg.getint("pdf", "ocr_min_text_threshold", fallback=50)

            # Detect if PDF is image-only or has minimal text
            needs_ocr = (not text or len(text.strip()) < ocr_min_text_threshold) and ocr_enabled

            if needs_ocr:
                log.info(
                    f"PDF has minimal text ({len(text.strip()) if text else 0} chars), attempting OCR"
                )
                try:
                    import pytesseract
                    from pdf2image import convert_from_bytes

                    # Get OCR configuration
                    ocr_dpi = cfg.getint("pdf", "ocr_dpi", fallback=300)
                    ocr_lang = cfg.get("pdf", "ocr_lang", fallback="eng")
                    ocr_psm = cfg.getint("pdf", "ocr_psm", fallback=3)

                    # Configure tesseract if custom path is specified
                    tesseract_cmd = cfg.get("pdf", "tesseract_cmd", fallback=None)
                    if tesseract_cmd:
                        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

                    # Convert PDF pages to images
                    log.debug(f"Converting PDF to images at {ocr_dpi} DPI")
                    images = convert_from_bytes(content, dpi=ocr_dpi)

                    # Extract text from each page using OCR
                    ocr_text_parts = []
                    if metadata:
                        meta_str = "\n".join(f"{k}: {v}" for k, v in metadata.items())
                        ocr_text_parts.append(f"[Metadata]\n{meta_str}\n")

                    # Configure OCR with PSM (Page Segmentation Mode)
                    # PSM 3 = Fully automatic page segmentation (default)
                    # PSM 6 = Assume a single uniform block of text
                    # PSM 11 = Sparse text - Find as much text as possible in no particular order
                    custom_config = f"--psm {ocr_psm}"

                    for page_num, image in enumerate(images, 1):
                        try:
                            page_ocr_text = pytesseract.image_to_string(
                                image, lang=ocr_lang, config=custom_config
                            )
                            if page_ocr_text and page_ocr_text.strip():
                                ocr_text_parts.append(
                                    f"[Page {page_num} (OCR)]\n{page_ocr_text.strip()}"
                                )
                                log.debug(
                                    f"OCR extracted {len(page_ocr_text.strip())} chars from page {page_num}"
                                )
                        except Exception as page_error:
                            log.warning(f"OCR failed for page {page_num}: {page_error}")
                            continue

                    # Use OCR text if extraction was successful
                    if ocr_text_parts:
                        ocr_text = "\n\n".join(ocr_text_parts)
                        if len(ocr_text.strip()) > len(text.strip() if text else ""):
                            log.info(
                                f"OCR extracted {len(ocr_text.strip())} chars (vs {len(text.strip()) if text else 0} from standard extraction)"
                            )
                            text = ocr_text
                        else:
                            log.info("OCR did not improve extraction, using standard extraction")
                    else:
                        log.warning("OCR produced no text")

                except ImportError as ocr_import_error:
                    missing_lib = str(ocr_import_error)
                    if "pytesseract" in missing_lib:
                        log.warning(
                            "pytesseract not installed, skipping OCR. Install with: pip install pytesseract"
                        )
                    elif "pdf2image" in missing_lib:
                        log.warning(
                            "pdf2image not installed, skipping OCR. Install with: pip install pdf2image"
                        )
                    else:
                        log.warning(f"OCR dependencies missing: {missing_lib}")
                except Exception as ocr_error:
                    log.warning(f"OCR extraction failed: {ocr_error}")

            # Validate extracted content (after OCR attempt)
            if not text or not text.strip():
                raise HTTPException(
                    status_code=400,
                    detail="PDF extraction produced no text. The file may be empty, image-only, or malformed. "
                    "If this is an image-based PDF, ensure Tesseract OCR is installed and configured.",
                )

        except ImportError as e:
            missing_lib = "pdfplumber" if "pdfplumber" in str(e) else "pypdf"
            raise HTTPException(
                status_code=400,
                detail=f"PDF support not available. Install {missing_lib}: pip install {missing_lib}",
            ) from e
        except HTTPException:
            # Re-raise HTTP exceptions without wrapping
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"PDF parsing failed: {e}") from e

    elif file_ext == ".docx":
        # Handle DOCX (Microsoft Word)
        try:
            import zipfile

            from docx import Document
            from docx.opc.exceptions import PackageNotFoundError

            try:
                doc = Document(io.BytesIO(content))
            except PackageNotFoundError:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid DOCX file: file is corrupted or not a valid Word document",
                ) from None
            except zipfile.BadZipFile:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid DOCX file: file structure is corrupted (not a valid ZIP archive)",
                ) from None

            text_parts = []
            image_counter = 0

            for para in doc.paragraphs:
                para_text = para.text.strip()

                # Check for embedded images in this paragraph
                # Images are stored in runs within paragraphs
                for run in para.runs:
                    # Check if this run contains an image
                    if hasattr(run, "_element") and run._element is not None:
                        # Look for drawing elements that contain images
                        for drawing in run._element.findall(
                            ".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}drawing"
                        ):
                            image_counter += 1
                            # Extract image description if available (alt text)
                            desc_elems = drawing.findall(
                                ".//{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}docPr"
                            )

                            image_desc = f"[Image {image_counter}"
                            if desc_elems and desc_elems[0].get("descr"):
                                image_desc += f": {desc_elems[0].get('descr')}"
                            elif desc_elems and desc_elems[0].get("name"):
                                image_desc += f": {desc_elems[0].get('name')}"
                            image_desc += "]"

                            text_parts.append(image_desc)

                if para_text:
                    # Preserve heading structure
                    if para.style and para.style.name.startswith("Heading"):
                        text_parts.append(f"\n## {para_text}\n")
                    else:
                        text_parts.append(para_text)

            # Handle tables
            for doc_table in doc.tables:
                table_rows: list[str] = []
                for row in doc_table.rows:
                    row_text = " | ".join(cell.text.strip() for cell in row.cells)
                    if row_text:
                        table_rows.append(row_text)
                if table_rows:
                    text_parts.append("\n[Table]\n" + "\n".join(table_rows) + "\n")

            text = "\n\n".join(text_parts)

            # Check if document is empty
            if not text.strip():
                raise HTTPException(
                    status_code=400,
                    detail="DOCX file is empty or contains no extractable text",
                )

        except ImportError:
            raise HTTPException(
                status_code=400,
                detail="DOCX support not available. Install python-docx: pip install python-docx",
            ) from None
        except HTTPException:
            # Re-raise HTTP exceptions (our specific error messages)
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"DOCX parsing failed: {e}") from None

    elif file_ext == ".md":
        # Handle Markdown with proper parsing (#2667)
        try:
            import frontmatter
            from bs4 import BeautifulSoup
            from markdown import markdown

            # Parse frontmatter and content
            post = frontmatter.loads(content.decode("utf-8"))

            # Extract metadata from frontmatter (values stringified: `metadata`
            # is dict[str, str] in this scope and is only string-formatted below)
            metadata = {str(k): str(v) for k, v in post.metadata.items()} if post.metadata else {}

            # Convert markdown to plain text (strip HTML tags)
            # This preserves structure while making it searchable
            html = markdown(post.content)
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(separator="\n\n")

            # Prepend frontmatter as metadata section
            if metadata:
                meta_str = "\n".join(f"{k}: {v}" for k, v in metadata.items())
                text = f"[Metadata]\n{meta_str}\n\n{text}"

        except ImportError:
            # Fallback to plain text if libraries unavailable
            text = content.decode("utf-8")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Markdown parsing failed: {e}") from None

    elif file_ext == ".json":
        # JSON files stored as formatted text
        import json

        try:
            data = json.loads(content.decode("utf-8"))
            text = json.dumps(data, indent=2, ensure_ascii=False)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON file: {e}") from None

    elif file_ext == ".pptx":
        # Handle PowerPoint presentations
        try:
            from pptx import Presentation

            prs = Presentation(io.BytesIO(content))
            text_parts = []

            # Process each slide in order
            for slide_num, slide in enumerate(prs.slides, start=1):
                slide_texts = []

                # Extract text from all shapes in the slide
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        slide_texts.append(shape.text.strip())

                # Extract speaker notes if present
                if slide.has_notes_slide:
                    notes_slide = slide.notes_slide
                    if hasattr(notes_slide, "notes_text_frame") and notes_slide.notes_text_frame:
                        notes_text = notes_slide.notes_text_frame.text.strip()
                        if notes_text:
                            slide_texts.append(f"[Speaker Notes]\n{notes_text}")

                # Add slide content if any text was found
                if slide_texts:
                    slide_content = f"[Slide {slide_num}]\n" + "\n\n".join(slide_texts)
                    text_parts.append(slide_content)

            # Join all slides with double newlines
            text = "\n\n".join(text_parts) if text_parts else ""

            if not text:
                raise HTTPException(
                    status_code=400, detail="PPTX file contains no extractable text"
                )

        except ImportError:
            raise HTTPException(
                status_code=400,
                detail="PPTX support not available. Install python-pptx: pip install python-pptx",
            ) from None
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"PPTX parsing failed: {e}") from None

    elif file_ext == ".epub":
        # Handle ePUB eBooks
        try:
            import ebooklib
            from bs4 import BeautifulSoup
            from ebooklib import epub

            try:
                book = epub.read_epub(io.BytesIO(content))
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid ePUB file: {e}") from None

            text_parts = []

            # Helper to extract metadata values (handles tuple format from ebooklib)
            def extract_metadata_values(items):
                """Extract values from metadata items.

                ebooklib returns metadata as tuples (namespace, value).
                This helper extracts just the values.
                """
                values = []
                for item in items:
                    if isinstance(item, tuple):
                        # Tuple format: (namespace, value)
                        values.append(str(item[1] if len(item) > 1 else item[0]))
                    else:
                        # String format (fallback)
                        values.append(str(item))
                return values

            # Extract metadata
            metadata = {}
            if book.get_metadata("DC", "title"):
                metadata["Title"] = ", ".join(
                    extract_metadata_values(book.get_metadata("DC", "title"))
                )
            if book.get_metadata("DC", "creator"):
                metadata["Author"] = ", ".join(
                    extract_metadata_values(book.get_metadata("DC", "creator"))
                )
            if book.get_metadata("DC", "description"):
                metadata["Description"] = ", ".join(
                    extract_metadata_values(book.get_metadata("DC", "description"))
                )
            if book.get_metadata("DC", "publisher"):
                metadata["Publisher"] = ", ".join(
                    extract_metadata_values(book.get_metadata("DC", "publisher"))
                )
            if book.get_metadata("DC", "date"):
                metadata["Date"] = ", ".join(
                    extract_metadata_values(book.get_metadata("DC", "date"))
                )
            if book.get_metadata("DC", "language"):
                metadata["Language"] = ", ".join(
                    extract_metadata_values(book.get_metadata("DC", "language"))
                )

            # Add metadata section if available
            if metadata:
                meta_str = "\n".join(f"{k}: {v}" for k, v in metadata.items())
                text_parts.append(f"[Metadata]\n{meta_str}\n")

            # Process all items in the book
            chapter_num = 0
            has_content = False  # Track if we found actual content (not just metadata)
            for item in book.get_items():
                # Only process document items (XHTML content)
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    chapter_num += 1

                    # Parse HTML content
                    html_content = item.get_body_content()
                    if html_content:
                        soup = BeautifulSoup(html_content, "html.parser")

                        # Remove script and style elements
                        for script in soup(["script", "style"]):
                            script.decompose()

                        # Extract text with some structure preservation
                        chapter_texts = []

                        # Process headings to preserve structure
                        for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
                            heading_text = heading.get_text(strip=True)
                            if heading_text:
                                level = heading.name[1]  # Extract number from h1, h2, etc.
                                chapter_texts.append(f"{'#' * int(level)} {heading_text}")

                        # Extract all text content
                        text_content = soup.get_text(separator="\n")

                        # Clean up excessive whitespace while preserving paragraph breaks
                        lines = [line.strip() for line in text_content.splitlines()]
                        # Remove empty lines and duplicates from heading extraction
                        cleaned_lines = []
                        prev_line = ""
                        for line in lines:
                            if line and line != prev_line:
                                cleaned_lines.append(line)
                                prev_line = line

                        chapter_text = "\n\n".join(cleaned_lines)

                        # Only count as content if it has substantial text (>50 chars)
                        # This filters out navigation-only items that just contain titles
                        if chapter_text.strip() and len(chapter_text.strip()) > 50:
                            has_content = True  # Found actual content
                            # Add chapter marker for multi-chapter books
                            if (
                                chapter_num > 1
                                or len(list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))) > 1
                            ):
                                text_parts.append(f"[Chapter {chapter_num}]\n{chapter_text}")
                            else:
                                text_parts.append(chapter_text)

            # Validate that we have actual content, not just metadata
            if not has_content:
                raise HTTPException(
                    status_code=400,
                    detail="ePUB extraction produced no text. The file may be empty, image-only, or malformed.",
                )

            # Join all parts with double newlines
            text = "\n\n".join(text_parts) if text_parts else ""

        except ImportError:
            raise HTTPException(
                status_code=400,
                detail="ePUB support not available. Install ebooklib: pip install ebooklib",
            ) from None
        except HTTPException:
            # Re-raise HTTP exceptions without wrapping
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"ePUB parsing failed: {e}") from None

    elif file_ext in {".html", ".htm"}:
        # Handle HTML documents (#2666)
        try:
            from bs4 import BeautifulSoup

            # Decode HTML content
            try:
                html_content = content.decode("utf-8")
            except UnicodeDecodeError:
                # Try common fallback encodings
                for encoding in ["iso-8859-1", "windows-1252", "cp1252"]:
                    try:
                        html_content = content.decode(encoding)
                        break
                    except UnicodeDecodeError:
                        continue
                else:
                    raise HTTPException(
                        status_code=400, detail="Unable to decode HTML file. Unsupported encoding."
                    )

            # Parse HTML
            soup = BeautifulSoup(html_content, "html.parser")

            # Remove boilerplate and non-content elements
            for tag in soup(
                ["script", "style", "nav", "header", "footer", "aside", "iframe", "noscript"]
            ):
                tag.decompose()

            # Remove common ad and tracking elements
            for class_name in [
                "advertisement",
                "ad-container",
                "social-share",
                "comments",
                "sidebar",
            ]:
                for elem in soup.find_all(class_=class_name):
                    elem.decompose()

            text_parts = []

            # Extract metadata from meta tags
            metadata = {}

            # Page title
            title_tag = soup.find("title")
            if title_tag and title_tag.string:
                metadata["Title"] = title_tag.string.strip()

            # Meta tags
            meta_mappings = {
                "description": "Description",
                "author": "Author",
                "keywords": "Keywords",
                "og:title": "OG_Title",
                "og:description": "OG_Description",
            }

            for meta_name, metadata_key in meta_mappings.items():
                # Try name attribute
                meta_tag = soup.find("meta", attrs={"name": meta_name})
                if not meta_tag:
                    # Try property attribute (for Open Graph tags)
                    meta_tag = soup.find("meta", attrs={"property": meta_name})

                if meta_tag:
                    meta_content = meta_tag.get("content")
                    if meta_content and isinstance(meta_content, str):
                        metadata[metadata_key] = meta_content.strip()

            # Add metadata section if available
            if metadata:
                meta_str = "\n".join(f"{k}: {v}" for k, v in metadata.items())
                text_parts.append(f"[Metadata]\n{meta_str}\n")

            # Extract main content with semantic structure preservation
            # Try to find main content area (common patterns)
            main_content = (
                soup.find("main")
                or soup.find("article")
                or soup.find("div", class_="content")
                or soup.find("div", id="content")
                or soup.find("div", class_="main")
                or soup.find("div", id="main")
                or soup.body
                or soup
            )

            # Process content preserving structure
            content_parts = []

            # Extract headings with hierarchy
            for heading in main_content.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
                heading_text = heading.get_text(strip=True)
                if heading_text:
                    level = heading.name[1]  # Extract number from h1, h2, etc.
                    content_parts.append(f"{'#' * int(level)} {heading_text}")

            # Extract paragraphs and preserve block structure
            for elem in main_content.find_all(["p", "div", "section", "blockquote", "pre", "code"]):
                elem_text = elem.get_text(strip=True)

                # Skip empty elements
                if not elem_text:
                    continue

                # Skip if this is just a container with nested elements we'll process separately
                if elem.find(["p", "div", "section"]) and elem.name == "div":
                    continue

                # Format blockquotes
                if elem.name == "blockquote":
                    elem_text = "> " + elem_text

                # Format code blocks
                if elem.name in ["pre", "code"]:
                    elem_text = f"```\n{elem_text}\n```"

                content_parts.append(elem_text)

            # Extract list items with structure
            for ul in main_content.find_all("ul"):
                list_items = []
                for li in ul.find_all("li", recursive=False):
                    li_text = li.get_text(strip=True)
                    if li_text:
                        list_items.append(f"• {li_text}")
                if list_items:
                    content_parts.append("\n".join(list_items))

            for ol in main_content.find_all("ol"):
                list_items = []
                for idx, li in enumerate(ol.find_all("li", recursive=False), start=1):
                    li_text = li.get_text(strip=True)
                    if li_text:
                        list_items.append(f"{idx}. {li_text}")
                if list_items:
                    content_parts.append("\n".join(list_items))

            # Extract tables
            for html_table in main_content.find_all("table"):
                html_table_rows: list[str] = []
                for row in html_table.find_all("tr"):
                    cells = row.find_all(["th", "td"])
                    if cells:
                        row_text = " | ".join(cell.get_text(strip=True) for cell in cells)
                        if row_text:
                            html_table_rows.append(row_text)
                if html_table_rows:
                    content_parts.append("[Table]\n" + "\n".join(html_table_rows))

            # If we extracted structured content, use it
            if content_parts:
                # Remove duplicates (headings might be extracted twice)
                seen = set()
                unique_parts = []
                for part in content_parts:
                    if part not in seen:
                        unique_parts.append(part)
                        seen.add(part)
                text_parts.extend(unique_parts)
            else:
                # Fallback: extract all text from main content
                text_content = main_content.get_text(separator="\n")
                # Clean up excessive whitespace
                lines = [line.strip() for line in text_content.splitlines()]
                cleaned_lines = [line for line in lines if line]
                if cleaned_lines:
                    text_parts.append("\n\n".join(cleaned_lines))

            # Combine all parts
            text = "\n\n".join(text_parts)

            # Validate extracted content
            if not text or not text.strip():
                raise HTTPException(
                    status_code=400,
                    detail="HTML extraction produced no text. The file may be empty or contain only boilerplate.",
                )

        except ImportError:
            raise HTTPException(
                status_code=400,
                detail="HTML support not available. Install beautifulsoup4: pip install beautifulsoup4",
            ) from None
        except HTTPException:
            # Re-raise HTTP exceptions
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"HTML parsing failed: {e}") from None

    else:
        # Plain text
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as e:
            raise HTTPException(status_code=400, detail=f"File encoding error: {e}") from None

    # Use filename as doc_id if not provided (sanitize to prevent path traversal)
    safe_filename = os.path.basename(file.filename or "").strip() if file.filename else ""
    final_doc_id = doc_id or safe_filename or f"upload_{uuid.uuid4().hex[:8]}"

    # Ingest
    try:
        result = ingest_document(doc_id=final_doc_id, text=text)
        return RagIngestResponse(
            doc_id=final_doc_id,
            chunks_ingested=result["chunks_ingested"],
            status=result["status"],
            doc_hash=result["doc_hash"],
            previous_hash=result["previous_hash"],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}") from None


@api.post("/rag/index-repo")
def rag_index_repo(_request: Request, req: RagIndexRepoRequest) -> RagIndexRepoResponse:
    """Index a code repository for RAG."""
    from nyxgpt.rag.rag import ingest_repository

    try:
        extensions_set = set(req.extensions) if req.extensions else None

        result = ingest_repository(
            repo_path=req.repo_path,
            doc_id_prefix=req.doc_id_prefix,
            extensions=extensions_set,
            extract_docs_only=req.extract_docs_only,
            ensure_schema=req.ensure_schema,
            collection=req.collection,
        )

        return RagIndexRepoResponse(
            total_files=result["total_files"],
            total_chunks=result["total_chunks"],
            files=result["files"],
            doc_ids=result["doc_ids"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Repository indexing failed: {e}") from e


# --- Log viewing endpoints ---


@api.get("/logs/files")
def logs_list_files(request: Request) -> dict[str, Any]:
    """List available log files with metadata."""
    cfg = _req_cfg(request)
    log_dir = get_log_dir(cfg)

    if not log_dir.exists():
        return {"files": []}

    files = []
    for log_file in sorted(log_dir.glob("*.log*"), reverse=True):
        if log_file.is_file():
            stat = log_file.stat()
            files.append(
                {
                    "name": log_file.name,
                    "path": str(log_file),
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                }
            )

    return {"files": files, "log_dir": str(log_dir)}


@api.get("/logs/view/{filename}")
def logs_view_file(
    request: Request,
    filename: str,
    tail: int | None = None,
    level: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    """
    View log file contents with optional filtering.

    Args:
        filename: Name of the log file (e.g., "nyxgpt.log")
        tail: Number of lines to return from the end (default: all)
        level: Filter by log level (DEBUG, INFO, WARNING, ERROR)
        search: Search string to filter lines
    """
    cfg = _req_cfg(request)
    log_dir = get_log_dir(cfg)

    # Sanitize filename to prevent path traversal
    safe_filename = os.path.basename(filename)
    log_file = log_dir / safe_filename

    # Validate file exists and is within log directory (or is a valid symlink from log directory)
    try:
        # First check that the file (or symlink) exists in the log directory
        if not log_file.exists():
            raise HTTPException(status_code=404, detail="Log file not found")

        if not log_file.is_file():
            raise HTTPException(status_code=404, detail="Log file not found")

        # Security check: Ensure the file path (before resolving symlinks) is in log_dir
        # This prevents path traversal while allowing symlinks that exist in the log directory
        log_dir_resolved = log_dir.resolve()
        log_file_parent = log_file.parent.resolve()

        if log_file_parent != log_dir_resolved:
            raise HTTPException(status_code=403, detail="Access denied")

    except HTTPException:
        raise
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail="Log file not found") from None

    try:
        with open(log_file, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        # Apply filters
        filtered_lines = lines

        # Filter by log level
        if level:
            level_upper = level.upper()
            filtered_lines = [line for line in filtered_lines if level_upper in line]

        # Filter by search string
        if search:
            search_lower = search.lower()
            filtered_lines = [line for line in filtered_lines if search_lower in line.lower()]

        # Apply tail limit
        if tail and tail > 0:
            filtered_lines = filtered_lines[-tail:]

        return {
            "filename": safe_filename,
            "lines": filtered_lines,
            "total_lines": len(lines),
            "filtered_lines": len(filtered_lines),
        }

    except Exception as e:
        log.error(f"Failed to read log file {log_file}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to read log file: {e}") from e


@api.get("/logs/stream/{filename}")
async def logs_stream_file(
    request: Request,
    filename: str,
    level: str | None = None,
    search: str | None = None,
) -> StreamingResponse:
    """
    Stream log file contents with optional filtering.

    Useful for real-time log viewing.
    """
    cfg = _req_cfg(request)
    log_dir = get_log_dir(cfg)

    # Sanitize filename to prevent path traversal
    safe_filename = os.path.basename(filename)
    log_file = log_dir / safe_filename

    # Validate file exists and is within log directory (or is a valid symlink from log directory)
    try:
        # First check that the file (or symlink) exists in the log directory
        if not log_file.exists():
            raise HTTPException(status_code=404, detail="Log file not found")

        if not log_file.is_file():
            raise HTTPException(status_code=404, detail="Log file not found")

        # Security check: Ensure the file path (before resolving symlinks) is in log_dir
        # This prevents path traversal while allowing symlinks that exist in the log directory
        log_dir_resolved = log_dir.resolve()
        log_file_parent = log_file.parent.resolve()

        if log_file_parent != log_dir_resolved:
            raise HTTPException(status_code=403, detail="Access denied")

    except HTTPException:
        raise
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail="Log file not found") from None

    async def stream_lines():
        """Generator that streams filtered log lines."""
        try:
            with open(log_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    # Apply filters
                    if level and level.upper() not in line:
                        continue
                    if search and search.lower() not in line.lower():
                        continue

                    yield line
        except Exception as e:
            log.error(f"Failed to stream log file {log_file}: {e}")
            yield f"Error: {e}\n"

    return StreamingResponse(
        stream_lines(),
        media_type="text/plain",
        headers={
            "Content-Disposition": f'inline; filename="{safe_filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


app.include_router(api)
