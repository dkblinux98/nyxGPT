"""Typed accessors for nyxGPT's ``config.ini`` settings.

Wraps a stdlib ``ConfigParser`` with cached, hot-reloadable loading
(`load_config`) plus one small getter per setting so callers never read
raw section/option strings directly. Every getter documents which
``[section] option`` it reads and its fallback default, and swallows
*value* errors by falling back to that default rather than raising.

A file-level parse error is the one thing not swallowed: `load_config`
raises `ConfigParseError` naming the file, the fault and the line (#3944).
There is no default to fall back to when the file itself cannot be read, and
the alternative -- letting a bare `configparser` error escape into the API's
catch-all handler -- reported a dead API as "Internal server error".

Note that ``ConfigParser`` applies ``optionxform = str.lower`` to option
names, so config.ini **option names are case-insensitive** (``API_KEY`` and
``api_key`` are one option, and writing both is a duplicate the file will not
load). Section names are case-sensitive.
"""

from __future__ import annotations

import configparser
import ipaddress
import logging
import os
import re
import secrets
import sys
import tempfile
from configparser import ConfigParser
from pathlib import Path

from nyxgpt import cloud_secrets

DEFAULT_CONFIG_PATH = Path.home() / ".nyxGPT" / "config.ini"

_CACHED_CFG: ConfigParser | None = None
_CACHED_PATH: Path | None = None
_CACHED_MTIME_NS: int | None = None

_logger = logging.getLogger(__name__)
_fallback_warned_keys: set[str] = set()


def _log_fallback_once(key: str, message: str, *, level: int = logging.WARNING) -> None:
    """Log that a config fallback engaged for ``key``, once per key per process.

    Getters call this from their except-block instead of silently returning a
    default, so a bad/missing value is visible in the logs rather than only
    showing up later as unexplained default behavior.
    """
    if key in _fallback_warned_keys:
        return
    _fallback_warned_keys.add(key)
    _logger.log(level, message)


def reset_fallback_warnings() -> None:
    """Clear the once-per-key fallback warning dedup set (test helper)."""
    _fallback_warned_keys.clear()


#: ``scheme://userinfo@`` -- the only part of a URL that carries a credential.
#: ``[^/@]`` cannot cross a path separator, so a literal ``@`` in a path
#: (``http://host/a@b``) is left alone.
_URL_USERINFO = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^/@]+)@")


def redact_url_userinfo(url: str) -> str:
    """Return ``url`` with any ``user:password@`` component replaced by ``***@``.

    Config values that name a *host* are routinely written as full URLs, and
    a full URL may legally carry credentials -- ``http://user:pass@ollama``
    is a valid `[ollama] base_url`. Error messages quoting such a value back
    at the operator would print the password to stderr and the logs.

    Called before interpolating any configured URL into a message (#3837,
    CodeQL #116). The redaction is unconditional rather than conditional on
    "does this deployment use credentials today": that question has to be
    re-answered on every future edit, and the answer is not visible at the
    interpolation site.
    """
    return _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}***@", url)


class ConfigValidationError(Exception):
    """Raised when configuration validation fails."""

    pass


def validate_config(cfg: ConfigParser) -> list[str]:
    """Validate configuration and return list of errors.

    This function checks that all required configuration sections and
    options exist and contain valid values. It's designed to fail fast
    on startup rather than silently using fallback values that might
    hide configuration errors.

    Args:
        cfg: The ConfigParser instance to validate

    Returns:
        List of error messages (empty if valid)
    """
    errors: list[str] = []

    # Check required sections
    required_sections = ["nyxgpt", "ollama"]
    for section in required_sections:
        if not cfg.has_section(section):
            errors.append(f"Missing required section: [{section}]")

    # Validate API port if specified
    if cfg.has_option("api", "port"):
        try:
            port = cfg.getint("api", "port")
            if not (1024 <= port <= 65535):
                errors.append(f"Invalid api.port: {port} (must be 1024-65535)")
        except ValueError as e:
            errors.append(f"Invalid api.port: must be an integer ({e})")

    # Validate API host if specified
    if cfg.has_option("api", "host"):
        host = cfg.get("api", "host")
        if not host.strip():
            errors.append("Invalid api.host: cannot be empty")

    # Validate Ollama base URL
    if cfg.has_option("ollama", "base_url"):
        url = cfg.get("ollama", "base_url")
        if not url.startswith(("http://", "https://")):
            errors.append(
                f"Invalid ollama.base_url: {redact_url_userinfo(url)} "
                "(must start with http:// or https://)"
            )

    # Validate RAG numeric settings
    rag_int_settings = {
        "chat_top_k": (1, 100),
        "max_chunks": (1, 100),
        "chat_context_max_chars": (100, 100000),
        "chunk_size": (100, 10000),
        "chunk_overlap": (0, 5000),
    }

    for setting, (min_val, max_val) in rag_int_settings.items():
        if cfg.has_option("rag", setting):
            try:
                val = cfg.getint("rag", setting)
                if not (min_val <= val <= max_val):
                    errors.append(f"Invalid rag.{setting}: {val} (must be {min_val}-{max_val})")
            except ValueError as e:
                errors.append(f"Invalid rag.{setting}: must be an integer ({e})")

    # Validate chunk_overlap < chunk_size
    if cfg.has_option("rag", "chunk_size") and cfg.has_option("rag", "chunk_overlap"):
        chunk_size = cfg.getint("rag", "chunk_size", fallback=800)
        chunk_overlap = cfg.getint("rag", "chunk_overlap", fallback=100)
        if chunk_overlap >= chunk_size:
            errors.append(
                f"Invalid RAG config: chunk_overlap ({chunk_overlap}) "
                f"must be less than chunk_size ({chunk_size})"
            )

    # Validate context window settings
    if cfg.has_option("context", "default_window_size"):
        try:
            window_size = cfg.getint("context", "default_window_size")
            if window_size < 100:
                errors.append(
                    f"Invalid context.default_window_size: {window_size} " "(must be at least 100)"
                )
            elif window_size > 1000000:
                errors.append(
                    f"Invalid context.default_window_size: {window_size} "
                    "(must not exceed 1,000,000)"
                )
        except ValueError as e:
            errors.append(f"Invalid context.default_window_size: must be an integer ({e})")

    # Validate warning threshold
    if cfg.has_option("context", "warning_threshold"):
        try:
            threshold = cfg.getfloat("context", "warning_threshold")
            if not (0.0 <= threshold <= 1.0):
                errors.append(
                    f"Invalid context.warning_threshold: {threshold} "
                    "(must be between 0.0 and 1.0)"
                )
        except ValueError as e:
            errors.append(f"Invalid context.warning_threshold: must be a float ({e})")

    # Validate model-specific context window overrides
    if cfg.has_section("context"):
        for option in cfg.options("context"):
            if option.startswith("context_window_"):
                try:
                    window_size = cfg.getint("context", option)
                    if window_size < 100:
                        errors.append(
                            f"Invalid context.{option}: {window_size} " "(must be at least 100)"
                        )
                    elif window_size > 1000000:
                        errors.append(
                            f"Invalid context.{option}: {window_size} "
                            "(must not exceed 1,000,000)"
                        )
                except ValueError as e:
                    errors.append(f"Invalid context.{option}: must be an integer ({e})")

    # Validate the cloud secrets provider, if set (empty = local deploy, unchanged)
    if cfg.has_option("secrets", "provider"):
        provider = cfg.get("secrets", "provider").strip().lower()
        valid_providers = {"", cloud_secrets.SSM_PROVIDER, cloud_secrets.SECRETS_MANAGER_PROVIDER}
        if provider not in valid_providers:
            errors.append(
                f"Invalid secrets.provider: {provider!r} "
                f"(must be one of {sorted(p for p in valid_providers if p)}, or empty)"
            )

    return errors


class ConfigParseError(RuntimeError):
    """config.ini exists but ConfigParser cannot read it (#3944).

    `load_config` used to let `configparser` errors escape untouched, and the
    API's catch-all handler turned every one of them into the generic
    ``{"code": "internal_error", "message": "Internal server error"}``. That
    is what the owner saw when a wizard save wrote a duplicate option: the
    real cause -- a named file, a named error, a line number -- was thrown
    away at exactly the moment it was needed. This type carries all three, so
    a hand-edited or wizard-damaged config.ini reports what is wrong with it.
    """


def _display_config_path(config_path: Path | str) -> str:
    """Render `config_path` home-relative (``~/.nyxGPT/config.ini``) when it is.

    The parse diagnosis travels to unauthenticated callers (see
    `describe_config_parse_error`), and an absolute path under `/home` or
    `/Users` spells out the OS account name. Collapsing the home prefix keeps
    the message just as actionable -- the file is still named, and the user
    knows where their own home is -- while carrying no account identifier. A
    path outside home is left alone: it is the operator's explicit
    `NYXGPT_CONFIG` choice and shortening it would only obscure which file
    failed.
    """
    text = str(config_path)
    try:
        home = Path.home()
    except (RuntimeError, OSError):  # pragma: no cover - no home on this host
        return text
    try:
        return str(Path("~") / Path(text).relative_to(home))
    except ValueError:
        return text


def describe_config_parse_error(
    config_path: Path | str,
    exc: configparser.Error,
    *,
    include_line_text: bool = False,
) -> str:
    """Render `exc` as a one-line diagnosis naming the file, error and line.

    `configparser` spreads the line number over three different attributes
    depending on the subclass -- `lineno` (``DuplicateOptionError``,
    ``DuplicateSectionError``, ``MissingSectionHeaderError``) and the
    ``errors`` list of ``(lineno, line)`` pairs (``ParsingError``) -- and
    the base class has none. Normalising them here means every consumer
    (`load_config`, `nyxgpt ops doctor`, the wizard's pre-write check) says
    the same actionable thing instead of "Failed to parse <path>".

    **`include_line_text` defaults to False because this text reaches
    unauthenticated callers.** `config_unreadable_guard` is the API's
    outermost middleware and must be: `api_key_auth` itself calls
    `load_config` before it can read `[auth] enabled` or compare a key, so
    when config.ini is malformed there is no request on which auth is
    enforceable, and this diagnosis is the 500 body for anyone who can reach
    the port. The raw text of the offending line is exactly the thing that
    must not travel that channel -- a hand-edit that drops a `[section]`
    header makes the *following* line the offender, and that line can be
    ``api_key = sk-live-...``. Error class, line number and section/option
    *names* are safe and stay: they are schema, not values.

    So the file's own bytes are quoted only when a caller opts in, and the
    only caller that does is `nyxgpt ops doctor` -- a local command the user
    runs against their own file, which is where the recovery documentation
    already sends them. Redacted callers point at it rather than guessing.

    The path itself is rendered home-relative on *both* paths
    (`_display_config_path`): an absolute `/Users/<name>/.nyxGPT/config.ini`
    names the OS account, and that is the same pre-auth channel on a smaller
    scale. `~/.nyxGPT/config.ini` identifies the file just as well.
    """
    lineno = getattr(exc, "lineno", None)
    where = f" at line {lineno}" if isinstance(lineno, int) else ""

    if isinstance(exc, configparser.DuplicateOptionError):
        # The exact shape #3944 produced. Say the case-insensitivity out loud:
        # `exc.option` is already lowercased by `optionxform`, so a user
        # staring at `SLACK_BOT_TOKEN` and `slack_bot_token` in their file
        # has no other clue that those are one option as far as the app is
        # concerned. Names only -- no value is quoted, on either path.
        detail = (
            f"option {exc.option!r} in section {exc.section!r} is defined more than once "
            "(option names are case-insensitive, so SLACK_BOT_TOKEN and slack_bot_token "
            "are the same option)"
        )
    elif isinstance(exc, configparser.DuplicateSectionError):
        detail = f"section {exc.section!r} is defined more than once"
    elif isinstance(exc, configparser.MissingSectionHeaderError):
        detail = (
            f"{exc.line.strip()!r} appears before any [section] header"
            if include_line_text
            else "a setting appears before any [section] header"
        )
    elif isinstance(exc, configparser.ParsingError) and getattr(exc, "errors", None):
        where = ""
        if include_line_text:
            detail = "; ".join(f"line {n}: {text.strip()!r}" for n, text in exc.errors)
        else:
            numbers = ", ".join(str(n) for n, _ in exc.errors)
            plural = "lines" if len(exc.errors) > 1 else "line"
            detail = f"unreadable {plural} {numbers}"
    elif include_line_text:
        detail = " ".join(str(exc).split())
    else:
        # The base class and the interpolation subclasses put raw values in
        # `str(exc)` (`InterpolationMissingOptionError` carries `rawval`), so
        # the fallback is redacted too rather than trusted case by case.
        detail = "the file could not be read by ConfigParser"

    hint = (
        "Fix that line (or restore a known-good config.ini) and retry."
        if include_line_text
        else "Run `nyxgpt ops doctor` to see the offending line, fix it "
        "(or restore a known-good config.ini) and retry."
    )
    where_file = _display_config_path(config_path)
    return f"Cannot parse {where_file}: {type(exc).__name__}{where}: {detail}. {hint}"


def load_config(path: str | Path | None = None) -> ConfigParser:
    """Load config.ini from a path.

    - If `path` is None, uses DEFAULT_CONFIG_PATH.
    - If `path` is a string, expands `~` and environment variables.

    This function is intentionally *cached* and will reload automatically when
    the underlying file changes (mtime). This allows config.ini updates to take
    effect without restarting the API.

    Hot-reloadable settings include:
    - [nyxgpt] default_model
    - [rag] enable_chat_context

    Raises `FileNotFoundError` when the file is absent and `ConfigParseError`
    when it is present but malformed (#3944).
    """
    global _CACHED_CFG, _CACHED_PATH, _CACHED_MTIME_NS

    raw = path if path is not None else DEFAULT_CONFIG_PATH

    if isinstance(raw, Path):
        config_path = raw.expanduser()
    else:
        # Allow callers to pass strings (e.g., "~/.nyxGPT/config.ini").
        config_path = Path(os.path.expandvars(raw)).expanduser()

    if not config_path.exists():
        raise FileNotFoundError(
            f"Missing config file: {config_path}\n"
            "Create it at ~/.nyxGPT/config.ini using example.config.ini as a template."
        )

    try:
        mtime_ns = config_path.stat().st_mtime_ns
    except OSError as e:
        # If we can’t stat for some reason, fall back to always re-reading.
        _logger.debug("Could not stat %s, disabling hot-reload cache: %s", config_path, e)
        mtime_ns = None

    if (
        _CACHED_CFG is not None
        and config_path == _CACHED_PATH
        and _CACHED_MTIME_NS is not None
        and mtime_ns is not None
        and mtime_ns == _CACHED_MTIME_NS
    ):
        return _CACHED_CFG

    parser = ConfigParser()
    try:
        parser.read(config_path, encoding="utf-8")
    except configparser.Error as e:
        # Unguarded, this escaped as a bare `configparser` error and the API's
        # catch-all handler flattened it to "Internal server error" -- the
        # single least useful thing to tell a user whose config.ini has one
        # bad line in it (#3944). Name the file, the fault and the line.
        raise ConfigParseError(describe_config_parse_error(config_path, e)) from e

    # Validate configuration on first load only (not on hot-reload)
    # This prevents noisy validation errors on every config change
    if _CACHED_CFG is None:
        validation_errors = validate_config(parser)
        if validation_errors:
            error_msg = "Configuration validation failed:\n  " + "\n  ".join(validation_errors)
            # Print to stderr for visibility even if logging isn't set up yet
            print(f"ERROR: {error_msg}", file=sys.stderr)
            # Don't raise, just warn - allows system to start with fallback values
            # But makes configuration errors visible

    _CACHED_CFG = parser
    _CACHED_PATH = config_path
    _CACHED_MTIME_NS = mtime_ns

    return parser


def get_default_model(cfg: ConfigParser) -> str:
    """Return the configured default chat model.

    Single source of truth:
    - [nyxgpt] default_model

    Falls back to a sane default if missing.

    This setting is hot-reloadable via config.ini changes.
    """
    return cfg.get("nyxgpt", "default_model", fallback="qwen3.5:0.8b").strip()


def get_ollama_base_url(cfg: ConfigParser) -> str:
    """Return the Ollama server base URL (``[ollama] base_url``).

    Falls back to ``http://127.0.0.1:11434`` when unset.
    """
    return cfg.get("ollama", "base_url", fallback="http://127.0.0.1:11434")


def get_chat_timeout_seconds(cfg: ConfigParser) -> int:
    """Return the configured per-request chat timeout (``[nyxgpt] chat_timeout_seconds``)."""
    try:
        return cfg.getint("nyxgpt", "chat_timeout_seconds", fallback=300)
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid nyxgpt.chat_timeout_seconds in config, using 300: %s", e)
        return 180


def _expand_path(value: str) -> Path:
    """Expand ``~`` in a config path string and return a contained, resolved ``Path``.

    Sink-side barrier (CodeQL py/path-injection, alert #45): config values
    such as ``sessions_dir`` are remotely writable via the config API, so the
    resulting filesystem path must be validated the same way as the chat-body
    sessions-dir override in app.py (``os.path.realpath`` + string-prefix
    containment -- the CodeQL-recognised barrier for this rule;
    ``Path.relative_to()`` is NOT modelled as a sanitizer) before any caller
    can use it for filesystem I/O.

    Raises:
        ValueError: If the expanded path resolves outside the user's home
            directory or the system temp directory (the only roots nyxGPT
            data is ever allowed to live under).
    """
    # Keep the tainted value in string form until after the guard: building
    # an intermediate Path(value) is itself flagged as a sink (alert #107).
    real = os.path.realpath(os.path.expanduser(value))
    _home = os.path.realpath(os.path.expanduser("~"))
    _tmp = os.path.realpath(tempfile.gettempdir())
    # Each return below is controlled by exactly one condition: CodeQL's
    # barrier-guard analysis only credits a guard whose branch is dominated
    # by a single sanitizing comparison, never a disjunction or loop of them.
    if real.startswith(_home + os.sep):
        return Path(real)
    if real.startswith(_tmp + os.sep):
        return Path(real)
    raise ValueError(f"Configured path resolves outside the allowed data area: {value!r}")


def get_sessions_dir(cfg: ConfigParser) -> Path:
    """Return the sessions storage directory (``[nyxgpt] sessions_dir``).

    Falls back to ``~/.nyxGPT/sessions``. ``~`` is expanded in either case.
    """
    val = cfg.get("nyxgpt", "sessions_dir", fallback=str(Path.home() / ".nyxGPT" / "sessions"))
    return _expand_path(val)


VALID_SESSION_BACKENDS = ("file", "cassandra")


def get_session_backend(cfg: ConfigParser) -> str:
    """Return the chat-session storage backend: ``"file"`` or ``"cassandra"``.

    Resolution order:
    1. ``NYXGPT_SESSION_BACKEND`` environment variable (container/k8s override)
    2. ``[nyxgpt] session_backend`` in config.ini
    3. ``"file"`` (back-compat default)

    ``"cassandra"`` stores sessions in the stack's Cassandra (see
    ``nyxgpt.session_db``), giving every deployment mode pointed at the same
    Cassandra an identical session list. With the ``"file"`` backend the
    legacy ``[nyxgpt] sessions_dir`` JSON-file store is used; that setting is
    deprecated for anything beyond the single-host native mode -- see
    docs/session-storage.md.

    An unrecognized value logs a warning and falls back to ``"file"``.
    """
    raw = os.environ.get("NYXGPT_SESSION_BACKEND", "").strip().lower()
    if not raw:
        try:
            raw = cfg.get("nyxgpt", "session_backend", fallback="file").strip().lower()
        except Exception:
            raw = "file"
    if raw not in VALID_SESSION_BACKENDS:
        _logger.warning(
            "Invalid session_backend %r (expected one of %s); using 'file'",
            raw,
            ", ".join(VALID_SESSION_BACKENDS),
        )
        return "file"
    return raw


def get_vectorstore_dir(cfg: ConfigParser) -> Path:
    """Return the vectorstore storage directory (``[nyxgpt] vectorstore_dir``).

    Falls back to ``~/.nyxGPT/vectorstore``. ``~`` is expanded in either case.
    """
    val = cfg.get(
        "nyxgpt", "vectorstore_dir", fallback=str(Path.home() / ".nyxGPT" / "vectorstore")
    )
    return _expand_path(val)


def get_api_host(cfg: ConfigParser) -> str:
    """Return the API bind host (``[api] host``), falling back to ``127.0.0.1``."""
    return cfg.get("api", "host", fallback="127.0.0.1")


def is_loopback_host(host: str) -> bool:
    """Return whether ``host`` only ever resolves within this machine.

    True for ``localhost`` and any loopback IP (``127.0.0.0/8``, ``::1``).
    Anything else -- ``0.0.0.0``, a LAN/public IP, a non-loopback hostname --
    is a bind that other machines can potentially reach.
    """
    host = host.strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def get_auth_enabled(cfg: ConfigParser) -> bool:
    """Return whether shared-secret API auth is enabled (``[auth] enabled``).

    Disabled by default (local-first, single-trusted-user posture).
    """
    try:
        return cfg.getboolean("auth", "enabled", fallback=False)
    except ValueError as e:
        _log_fallback_once("auth.enabled", f"Invalid auth.enabled in config, using False: {e}")
        return False


def validate_bind_security(cfg: ConfigParser) -> str | None:
    """Return an actionable error message if the API would bind non-loopback with auth off.

    A non-loopback ``[api] host`` (``0.0.0.0``, a LAN IP, ...) combined with
    ``[auth] enabled`` unset or false lets anyone who can reach this host or
    network call the API with no credentials -- exposure without auth must be
    impossible, not just discouraged (see ``docs/security.md``). Returns
    ``None`` when the bind is loopback-only, or when auth is enabled.
    """
    host = get_api_host(cfg)
    if is_loopback_host(host):
        return None
    if get_auth_enabled(cfg):
        return None
    return (
        f"Refusing to start: [api] host = {host} is not loopback-only, but "
        "[auth] enabled is not set to true. Binding a non-loopback address "
        "without authentication would let anyone who can reach this host or "
        "network call the API with no credentials. Fix: run `nyxgpt wizard` "
        "to set [auth] enabled = true with a generated api_key, or set "
        "[api] host back to 127.0.0.1 for local-only use."
    )


def get_api_port(cfg: ConfigParser) -> int:
    """Return the API bind port (``[api] port``).

    Falls back to ``8000`` and logs a warning if the configured value
    isn't a valid integer.
    """
    try:
        return cfg.getint("api", "port", fallback=8000)
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid api.port in config, using default 8000: %s", e)
        return 8000


def get_canary_namespace(cfg: ConfigParser) -> str:
    """Return the Kubernetes namespace used for canary rollouts (``[canary] namespace``).

    Falls back to ``"nyxgpt"``.
    """
    return cfg.get("canary", "namespace", fallback="nyxgpt")


def get_canary_total_replicas(cfg: ConfigParser) -> int:
    """Return the replica ceiling a canary rollout may grow a track to.

    Reads ``[canary] total_replicas``, clamped to a minimum of 1. Falls
    back to 4 (also used on parse errors).

    Since #3833 this is a **ceiling on borrowing**, not a standing pool
    size: the stable Deployments rest at 1 replica and ``nyxgpt canary
    start`` grows a track to at most this many Pods for the rollout's
    duration, handing them back on promote/rollback. A larger value buys
    finer weight steps (4 makes 25% expressible) at the cost of the Pods a
    running rollout has to be able to schedule -- nothing while idle.
    """
    try:
        return max(1, cfg.getint("canary", "total_replicas", fallback=4))
    except (ValueError, TypeError):
        return 4


def get_canary_step_percent(cfg: ConfigParser) -> int:
    """Return the percentage of traffic shifted per canary step.

    Reads ``[canary] step_percent``, clamped to 1-100. Falls back to 25
    (also used on parse errors).
    """
    try:
        return min(100, max(1, cfg.getint("canary", "step_percent", fallback=25)))
    except (ValueError, TypeError):
        return 25


def get_canary_error_rate_threshold(cfg: ConfigParser) -> float:
    """Return the error-rate percentage above which a canary is rolled back.

    Reads ``[canary] error_rate_threshold_percent``. Falls back to 5.0
    (also used on parse errors).
    """
    try:
        return cfg.getfloat("canary", "error_rate_threshold_percent", fallback=5.0)
    except (ValueError, TypeError):
        return 5.0


def get_canary_latency_p95_threshold_ms(cfg: ConfigParser) -> float:
    """Return the p95 latency (ms) above which a canary is rolled back.

    Reads ``[canary] latency_p95_threshold_ms``. Falls back to 2000.0
    (also used on parse errors).
    """
    try:
        return cfg.getfloat("canary", "latency_p95_threshold_ms", fallback=2000.0)
    except (ValueError, TypeError):
        return 2000.0


def get_canary_min_requests(cfg: ConfigParser) -> int:
    """Return the minimum request count before evaluating canary health.

    Reads ``[canary] min_requests_for_evaluation``, clamped to a minimum
    of 1. Falls back to 20 (also used on parse errors).
    """
    try:
        return max(1, cfg.getint("canary", "min_requests_for_evaluation", fallback=20))
    except (ValueError, TypeError):
        return 20


def get_rag_enabled(cfg: ConfigParser) -> bool:
    """Primary RAG on/off switch.

    Single source of truth, used by both the chat runtime (RAG context
    injection) and the admin health/overview/config endpoints, so they
    never disagree about whether RAG is active:
    - [rag] enable_chat_context

    Legacy alias (deprecated, only consulted when `enable_chat_context`
    is not explicitly set):
    - [rag] enabled
    """
    import logging

    log = logging.getLogger(__name__)

    try:
        if cfg.has_option("rag", "enable_chat_context"):
            return cfg.getboolean("rag", "enable_chat_context")
    except (ValueError, TypeError) as e:
        log.warning("Invalid rag.enable_chat_context in config, checking legacy setting: %s", e)
        # Fall through to legacy key.
        pass

    # Legacy alias (kept for compatibility with earlier docs/tests).
    return _get_rag_enabled_legacy_alias(cfg)


def _get_rag_enabled_legacy_alias(cfg: ConfigParser) -> bool:
    """Legacy compatibility key.

    Some older configs/docs referred to this setting as:
    - [rag] enabled

    Prefer `get_rag_enabled()` / `[rag] enable_chat_context` going forward.
    """
    try:
        return cfg.getboolean("rag", "enabled", fallback=False)
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid rag.enabled in config, using False: %s", e)
        return False


def get_rag_chat_top_k(cfg: ConfigParser) -> int:
    """Return the number of top-scoring chunks to retrieve per RAG query.

    Reads ``[rag] chat_top_k``. Falls back to 3 (also used on parse errors).
    """
    try:
        return cfg.getint("rag", "chat_top_k", fallback=3)
    except Exception:
        _log_fallback_once("rag.chat_top_k", "Invalid rag.chat_top_k in config, using default 3")
        return 3


def get_rag_min_score(cfg: ConfigParser) -> float:
    """Return the minimum similarity score for a RAG chunk to be kept.

    Reads ``[rag] min_score``. Falls back to 0.0 (also used on parse errors).
    """
    try:
        return cfg.getfloat("rag", "min_score", fallback=0.0)
    except Exception:
        _log_fallback_once("rag.min_score", "Invalid rag.min_score in config, using default 0.0")
        return 0.0


def get_rag_max_chunks(cfg: ConfigParser) -> int:
    """Return the maximum number of RAG chunks injected into chat context.

    Reads ``[rag] max_chunks``. Falls back to 6 (also used on parse errors).
    """
    try:
        return cfg.getint("rag", "max_chunks", fallback=6)
    except Exception:
        _log_fallback_once("rag.max_chunks", "Invalid rag.max_chunks in config, using default 6")
        return 6


def get_rag_chat_context_max_chars(cfg: ConfigParser) -> int:
    """Return the max character budget for RAG context injected into chat.

    Reads ``[rag] chat_context_max_chars``. Falls back to 2400 (also used
    on parse errors).
    """
    try:
        return cfg.getint("rag", "chat_context_max_chars", fallback=2400)
    except Exception:
        _log_fallback_once(
            "rag.chat_context_max_chars",
            "Invalid rag.chat_context_max_chars in config, using default 2400",
        )
        return 2400


def get_rag_dedupe(cfg: ConfigParser) -> bool:
    """Return whether duplicate/near-duplicate RAG chunks are removed.

    Reads ``[rag] dedupe``. Falls back to True (also used on parse errors).
    """
    try:
        return cfg.getboolean("rag", "dedupe", fallback=True)
    except Exception:
        _log_fallback_once("rag.dedupe", "Invalid rag.dedupe in config, using default True")
        return True


def get_rag_include_scores(cfg: ConfigParser) -> bool:
    """Return whether similarity scores are included in RAG chat context.

    Reads ``[rag] include_scores``. Falls back to False (also used on
    parse errors).
    """
    try:
        return cfg.getboolean("rag", "include_scores", fallback=False)
    except Exception:
        _log_fallback_once(
            "rag.include_scores", "Invalid rag.include_scores in config, using default False"
        )
        return False


def get_rag_include_headers(cfg: ConfigParser) -> bool:
    """Return whether source headers are included in RAG chat context.

    Reads ``[rag] include_headers``. Falls back to True (also used on
    parse errors).
    """
    try:
        return cfg.getboolean("rag", "include_headers", fallback=True)
    except Exception:
        _log_fallback_once(
            "rag.include_headers", "Invalid rag.include_headers in config, using default True"
        )
        return True


def get_rag_debug_mode(cfg: ConfigParser) -> bool:
    """Get RAG debug mode flag.

    When enabled, RAG operations collect and return detailed debug information
    including timing metrics, query analysis, embedding details, and filtering stats.

    Args:
        cfg: ConfigParser instance

    Returns:
        True if debug mode is enabled, False otherwise
    """
    try:
        return cfg.getboolean("rag", "debug_mode", fallback=False)
    except Exception:
        _log_fallback_once(
            "rag.debug_mode", "Invalid rag.debug_mode in config, using default False"
        )
        return False


def get_rag_good_score_threshold(cfg: ConfigParser) -> float:
    """Get the threshold for 'good' RAG similarity scores.

    Scores >= this threshold are considered high confidence and displayed
    with green visual indicators in the UI.

    Args:
        cfg: ConfigParser instance

    Returns:
        Good score threshold (default: 0.7)
    """
    try:
        return cfg.getfloat("rag", "good_score_threshold", fallback=0.7)
    except Exception:
        _log_fallback_once(
            "rag.good_score_threshold",
            "Invalid rag.good_score_threshold in config, using default 0.7",
        )
        return 0.7


def get_rag_medium_score_threshold(cfg: ConfigParser) -> float:
    """Get the threshold for 'medium' RAG similarity scores.

    Scores >= this threshold but < good_score_threshold are considered
    medium confidence and displayed with yellow visual indicators in the UI.
    Scores < this threshold are considered low confidence (red).

    Args:
        cfg: ConfigParser instance

    Returns:
        Medium score threshold (default: 0.4)
    """
    try:
        return cfg.getfloat("rag", "medium_score_threshold", fallback=0.4)
    except Exception:
        _log_fallback_once(
            "rag.medium_score_threshold",
            "Invalid rag.medium_score_threshold in config, using default 0.4",
        )
        return 0.4


def get_rate_limit_enabled(cfg: ConfigParser) -> bool:
    """Get rate limiting enabled flag.

    Returns whether rate limiting is enabled for the API.
    Disabled by default (localhost-only app).

    Args:
        cfg: ConfigParser instance

    Returns:
        True if rate limiting is enabled, False otherwise
    """
    try:
        return cfg.getboolean("rate_limit", "enabled", fallback=False)
    except Exception:
        _log_fallback_once(
            "rate_limit.enabled", "Invalid rate_limit.enabled in config, using default False"
        )
        return False


def get_rate_limit_config(cfg: ConfigParser) -> dict:
    """Get rate limiting configuration.

    Returns all rate limiting settings as a dictionary.
    Uses sensible defaults for localhost deployment.

    Args:
        cfg: ConfigParser instance

    Returns:
        Dictionary with keys: requests_per_second, burst_size
    """
    try:
        return {
            "requests_per_second": cfg.getint("rate_limit", "requests_per_second", fallback=10),
            "burst_size": cfg.getint("rate_limit", "burst_size", fallback=20),
        }
    except Exception:
        _log_fallback_once(
            "rate_limit.config",
            "Invalid rate_limit.requests_per_second/burst_size in config, using defaults " "10/20",
        )
        # Return defaults if any parsing errors
        return {
            "requests_per_second": 10,
            "burst_size": 20,
        }


def get_context_window_size(cfg: ConfigParser, model: str | None = None) -> int:
    """Get maximum context window size for a model.

    Checks model-specific override first, then falls back to default.
    Common context window sizes:
    - llama3.1:8b, llama3.1:70b: 128k tokens
    - llama2: 4k tokens
    - mistral: 8k tokens
    - qwen2.5: 32k tokens

    Args:
        cfg: ConfigParser instance
        model: Optional model name to look up specific limit

    Returns:
        Maximum context window size in tokens
    """
    # Try model-specific setting first
    if model:
        # Sanitize model name for config key (replace : and . with _)
        safe_model = model.replace(":", "_").replace(".", "_").replace("/", "_")
        option = f"context_window_{safe_model}"
        if cfg.has_option("context", option):
            try:
                return cfg.getint("context", option)
            except Exception:
                _log_fallback_once(
                    f"context.{option}",
                    f"Invalid context.{option} in config, falling back to "
                    "context.default_window_size",
                )

    # Fall back to default
    try:
        return cfg.getint("context", "default_window_size", fallback=8192)
    except Exception:
        _log_fallback_once(
            "context.default_window_size",
            "Invalid context.default_window_size in config, using default 8192",
        )
        return 8192


def get_context_warning_threshold(cfg: ConfigParser) -> float:
    """Get threshold for warning when approaching context limit.

    Returns fraction of context window (0.0-1.0) at which to log warnings.

    Args:
        cfg: ConfigParser instance

    Returns:
        Warning threshold as fraction (default 0.8 = 80%)
    """
    try:
        threshold = cfg.getfloat("context", "warning_threshold", fallback=0.8)
        # Clamp to valid range
        return max(0.0, min(1.0, threshold))
    except Exception:
        _log_fallback_once(
            "context.warning_threshold",
            "Invalid context.warning_threshold in config, using default 0.8",
        )
        return 0.8


def get_system_prompt_minimize(cfg: ConfigParser) -> bool:
    """Get system prompt minimization setting.

    When enabled, system prompts are optimized to reduce token usage by:
    - Removing redundant whitespace and formatting
    - Condensing verbose patterns
    - Preserving semantic meaning

    Args:
        cfg: ConfigParser instance

    Returns:
        True if minimization is enabled, False otherwise (default)
    """
    try:
        return cfg.getboolean("nyxgpt", "system_prompt_minimize", fallback=False)
    except Exception:
        _log_fallback_once(
            "nyxgpt.system_prompt_minimize",
            "Invalid nyxgpt.system_prompt_minimize in config, using default False",
        )
        return False


def get_rag_instruction_template(cfg: ConfigParser) -> str:
    """Get RAG instruction template for the LLM.

    This template tells the model how to use retrieved context.
    Supports template variables: {context}

    Args:
        cfg: ConfigParser instance

    Returns:
        Instruction template string
    """
    default_template = (
        "Use the retrieved context below when it is relevant and helpful. "
        "Do not mention that you were given retrieved context unless the user explicitly asks about sources. "
        "If the context is insufficient, say so and answer from general knowledge.\n\n"
        "{context}"
    )
    try:
        return cfg.get("rag", "instruction_template", fallback=default_template)
    except Exception:
        _log_fallback_once(
            "rag.instruction_template",
            "Invalid rag.instruction_template in config, using built-in default",
        )
        return default_template


def get_rag_context_format(cfg: ConfigParser) -> str:
    """Get RAG context format template.

    This template wraps the retrieved context chunks.
    Supports template variables: {context}

    Args:
        cfg: ConfigParser instance

    Returns:
        Context format template string
    """
    default_format = "--- BEGIN RETRIEVED CONTEXT ---\n{context}\n--- END RETRIEVED CONTEXT ---"
    try:
        return cfg.get("rag", "context_format", fallback=default_format)
    except Exception:
        _log_fallback_once(
            "rag.context_format", "Invalid rag.context_format in config, using built-in default"
        )
        return default_format


def get_prompt_mode_enabled(cfg: ConfigParser) -> bool:
    """Get whether adaptive prompt mode is enabled.

    When enabled, system prompts adapt based on conversation length
    (short/medium/long modes). Only applies when no custom system_prompt is set.

    Args:
        cfg: ConfigParser instance

    Returns:
        True if adaptive mode is enabled, False otherwise
    """
    try:
        return cfg.getboolean("prompt", "adaptive_mode_enabled", fallback=False)
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid prompt.adaptive_mode_enabled in config, using False: %s", e)
        return False


def get_prompt_mode_short_threshold(cfg: ConfigParser) -> int:
    """Get message count threshold for short prompt mode.

    Conversations with fewer messages than this threshold use short mode.

    Args:
        cfg: ConfigParser instance

    Returns:
        Message count threshold for short mode (default: 3)
    """
    try:
        threshold = cfg.getint("prompt", "short_threshold", fallback=3)
        return max(1, threshold)  # Must be at least 1
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid prompt.short_threshold in config, using 3: %s", e)
        return 3


def get_prompt_mode_long_threshold(cfg: ConfigParser) -> int:
    """Get message count threshold for long prompt mode.

    Conversations with this many messages or more use long mode.
    Between short_threshold and long_threshold, medium mode is used.

    Args:
        cfg: ConfigParser instance

    Returns:
        Message count threshold for long mode (default: 10)
    """
    try:
        threshold = cfg.getint("prompt", "long_threshold", fallback=10)
        # Ensure long_threshold is greater than short_threshold
        short_threshold = get_prompt_mode_short_threshold(cfg)
        return max(short_threshold + 1, threshold)
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid prompt.long_threshold in config, using 10: %s", e)
        return 10


def get_cassandra_pool_size(cfg: ConfigParser) -> int:
    """Get the Cassandra connection pool size (connections per host).

    Controls how many driver-level connections are maintained to each
    Cassandra host.  Higher values support more concurrent RAG queries.

    Args:
        cfg: ConfigParser instance

    Returns:
        Pool size (default: 2, range: 1-16)
    """
    try:
        size = cfg.getint("rag", "cassandra_pool_size", fallback=2)
        return max(1, min(16, size))
    except Exception:
        _log_fallback_once(
            "rag.cassandra_pool_size",
            "Invalid rag.cassandra_pool_size in config, using default 2",
        )
        return 2


def get_cassandra_health_check_interval(cfg: ConfigParser) -> float:
    """Get the interval between Cassandra connection health checks (seconds).

    Args:
        cfg: ConfigParser instance

    Returns:
        Health check interval in seconds (default: 30.0)
    """
    try:
        interval = cfg.getfloat("rag", "cassandra_health_check_interval", fallback=30.0)
        return max(5.0, min(300.0, interval))
    except Exception:
        _log_fallback_once(
            "rag.cassandra_health_check_interval",
            "Invalid rag.cassandra_health_check_interval in config, using default 30.0",
        )
        return 30.0


def get_cassandra_reconnect_max_attempts(cfg: ConfigParser) -> int:
    """Get the maximum number of Cassandra reconnection attempts.

    Args:
        cfg: ConfigParser instance

    Returns:
        Max reconnect attempts (default: 3, range: 1-10)
    """
    try:
        attempts = cfg.getint("rag", "cassandra_reconnect_max_attempts", fallback=3)
        return max(1, min(10, attempts))
    except Exception:
        _log_fallback_once(
            "rag.cassandra_reconnect_max_attempts",
            "Invalid rag.cassandra_reconnect_max_attempts in config, using default 3",
        )
        return 3


def get_cassandra_batch_size(cfg: ConfigParser) -> int:
    """Get the number of chunk upserts grouped into a single Cassandra batch.

    Larger batches reduce network round trips during ingestion at the cost of
    larger individual requests; the driver enforces its own batch size limits,
    so this is clamped to a conservative range.

    Args:
        cfg: ConfigParser instance

    Returns:
        Batch size (default: 20, range: 1-100)
    """
    try:
        size = cfg.getint("rag", "cassandra_batch_size", fallback=20)
        return max(1, min(100, size))
    except Exception:
        _log_fallback_once(
            "rag.cassandra_batch_size",
            "Invalid rag.cassandra_batch_size in config, using default 20",
        )
        return 20


def get_vector_similarity_function(cfg: ConfigParser) -> str:
    """Get the ANN similarity function used for vector search.

    Determines both how the Cassandra SAI vector index scores candidates
    (index build option) and which CQL similarity function is used to
    compute the returned score, so the two must stay in sync.

    Args:
        cfg: ConfigParser instance

    Returns:
        One of "cosine", "dot_product", "euclidean" (default: "cosine")
    """
    try:
        value = cfg.get("rag", "vector_similarity_function", fallback="cosine").strip().lower()
    except Exception:
        _log_fallback_once(
            "rag.vector_similarity_function",
            "Invalid rag.vector_similarity_function in config, using default cosine",
        )
        return "cosine"
    return value if value in ("cosine", "dot_product", "euclidean") else "cosine"


def get_ann_oversample_factor(cfg: ConfigParser) -> float:
    """Get the ANN oversampling factor for candidate retrieval.

    Multiplies the number of candidates fetched from the ANN index beyond
    what metadata filtering alone requires, trading additional query cost
    for improved recall on approximate search.

    Args:
        cfg: ConfigParser instance

    Returns:
        Oversampling factor (default: 1.0, range: 1.0-5.0)
    """
    try:
        factor = cfg.getfloat("rag", "ann_oversample_factor", fallback=1.0)
        return max(1.0, min(5.0, factor))
    except Exception:
        _log_fallback_once(
            "rag.ann_oversample_factor",
            "Invalid rag.ann_oversample_factor in config, using default 1.0",
        )
        return 1.0


def get_cassandra_batch_query_concurrency(cfg: ConfigParser) -> int:
    """Get the concurrency cap for batched ANN vector searches.

    Bounds how many ANN queries run in flight at once when searching
    multiple query embeddings in a single batch call, limiting peak
    memory and connection usage.

    Args:
        cfg: ConfigParser instance

    Returns:
        Concurrency cap (default: 4, range: 1-32)
    """
    try:
        concurrency = cfg.getint("rag", "cassandra_batch_query_concurrency", fallback=4)
        return max(1, min(32, concurrency))
    except Exception:
        _log_fallback_once(
            "rag.cassandra_batch_query_concurrency",
            "Invalid rag.cassandra_batch_query_concurrency in config, using default 4",
        )
        return 4


def get_batch_enabled(cfg: ConfigParser) -> bool:
    """Get whether request batching is enabled.

    When enabled, multiple chat/RAG requests can be batched together
    for improved throughput. Disabled by default.

    Args:
        cfg: ConfigParser instance

    Returns:
        True if batching is enabled, False otherwise
    """
    try:
        return cfg.getboolean("batch", "enabled", fallback=False)
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid batch.enabled in config, using False: %s", e)
        return False


def get_batch_size(cfg: ConfigParser) -> int:
    """Get maximum batch size for request batching.

    Maximum number of requests to batch together before processing.

    Args:
        cfg: ConfigParser instance

    Returns:
        Maximum batch size (default: 4, range: 1-50)
    """
    try:
        size = cfg.getint("batch", "batch_size", fallback=4)
        return max(1, min(50, size))  # Clamp to valid range
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid batch.batch_size in config, using 4: %s", e)
        return 4


def get_batch_wait_time_ms(cfg: ConfigParser) -> int:
    """Get maximum wait time for batch to fill.

    Maximum time to wait for batch to reach batch_size before
    processing whatever is available (milliseconds).

    Args:
        cfg: ConfigParser instance

    Returns:
        Wait time in milliseconds (default: 100ms, range: 10-5000ms)
    """
    try:
        wait_ms = cfg.getint("batch", "wait_time_ms", fallback=100)
        return max(10, min(5000, wait_ms))  # Clamp to valid range
    except (ValueError, TypeError) as e:
        import logging

        log = logging.getLogger(__name__)
        log.warning("Invalid batch.wait_time_ms in config, using 100: %s", e)
        return 100


def get_tracing_enabled(cfg: ConfigParser) -> bool:
    """Get whether distributed tracing is enabled.

    Enabled by default (2026-07-28 owner decision). When enabled, spans are
    exported via OTLP to a local collector (e.g. the `tracing` Compose
    profile's OTel collector + Jaeger all-in-one) -- traces never leave the
    machine. Span export degrades gracefully when the collector isn't
    reachable yet (see `tracing.init_tracing`); `nyxgpt ops doctor` is the
    diagnostic surface for that (#3350).

    Args:
        cfg: ConfigParser instance

    Returns:
        True if tracing is enabled, False otherwise
    """
    try:
        return cfg.getboolean("tracing", "enabled", fallback=True)
    except Exception:
        _log_fallback_once(
            "tracing.enabled", "Invalid tracing.enabled in config, using default True"
        )
        return True


def get_tracing_config(cfg: ConfigParser) -> dict:
    """Get distributed tracing configuration.

    Args:
        cfg: ConfigParser instance

    Returns:
        Dictionary with keys: enabled, service_name, otlp_endpoint, jaeger_ui_url
    """
    return {
        "enabled": get_tracing_enabled(cfg),
        "service_name": cfg.get("tracing", "service_name", fallback="nyxgpt-api"),
        "otlp_endpoint": cfg.get(
            "tracing", "otlp_endpoint", fallback="http://localhost:4318/v1/traces"
        ),
        "jaeger_ui_url": cfg.get("tracing", "jaeger_ui_url", fallback="http://localhost:16686"),
    }


def get_error_tracking_enabled(cfg: ConfigParser) -> bool:
    """Get whether error tracking is enabled.

    Disabled by default. When enabled (and a DSN is configured), exceptions
    are reported via the Sentry SDK protocol to a self-hosted, local-only
    tracker (e.g. the `errors` Compose profile's GlitchTip instance) --
    error data never leaves the machine.

    Args:
        cfg: ConfigParser instance

    Returns:
        True if error tracking is enabled, False otherwise
    """
    try:
        return cfg.getboolean("error_tracking", "enabled", fallback=False)
    except Exception:
        _log_fallback_once(
            "error_tracking.enabled",
            "Invalid error_tracking.enabled in config, using default False",
        )
        return False


def get_error_tracking_config(cfg: ConfigParser) -> dict:
    """Get error tracking configuration.

    Args:
        cfg: ConfigParser instance

    Returns:
        Dictionary with keys: enabled, dsn, environment, release,
        traces_sample_rate, glitchtip_ui_url
    """
    try:
        traces_sample_rate = cfg.getfloat("error_tracking", "traces_sample_rate", fallback=0.0)
    except ValueError:
        traces_sample_rate = 0.0

    return {
        "enabled": get_error_tracking_enabled(cfg),
        "dsn": cfg.get("error_tracking", "dsn", fallback=""),
        "environment": cfg.get("error_tracking", "environment", fallback="development"),
        "release": cfg.get("error_tracking", "release", fallback=""),
        "traces_sample_rate": traces_sample_rate,
        "glitchtip_ui_url": cfg.get(
            "error_tracking", "glitchtip_ui_url", fallback="http://localhost:8080"
        ),
    }


def get_monitoring_enabled(cfg: ConfigParser) -> bool:
    """Get whether the Grafana/Prometheus monitoring stack is enabled.

    Disabled by default. This flag doesn't start anything by itself -- it
    only controls whether the SRE/admin dashboard treats the `monitoring`
    Compose profile as running and shows the Grafana link.

    Args:
        cfg: ConfigParser instance

    Returns:
        True if monitoring is enabled, False otherwise
    """
    try:
        return cfg.getboolean("monitoring", "enabled", fallback=False)
    except Exception:
        _log_fallback_once(
            "monitoring.enabled", "Invalid monitoring.enabled in config, using default False"
        )
        return False


def get_monitoring_config(cfg: ConfigParser) -> dict:
    """Get Grafana/Prometheus monitoring configuration.

    Args:
        cfg: ConfigParser instance

    Returns:
        Dictionary with keys: enabled, grafana_ui_url, prometheus_ui_url
    """
    return {
        "enabled": get_monitoring_enabled(cfg),
        "grafana_ui_url": cfg.get("monitoring", "grafana_ui_url", fallback="http://localhost:3001"),
        "prometheus_ui_url": cfg.get(
            "monitoring", "prometheus_ui_url", fallback="http://localhost:9090"
        ),
    }


def get_monitoring_grafana_admin_password(cfg: ConfigParser) -> str:
    """Get the Grafana admin password, config.ini's single source of truth for it.

    Deliberately kept out of `get_monitoring_config` -- that dict is returned
    verbatim by `GET /api/v1/monitoring`, and this value must never be
    exposed over the API. It exists only so local tooling (`nyxgpt ops
    env-sync`) can derive the Compose `.env`'s `GRAFANA_ADMIN_PASSWORD` from
    config.ini instead of the user maintaining both separately.

    Args:
        cfg: ConfigParser instance

    Returns:
        The configured password, or "" if unset
    """
    return cfg.get("monitoring", "grafana_admin_password", fallback="")


def grafana_admin_password_path() -> Path:
    """Where the ops-managed Grafana admin password is stored, for when
    `[monitoring] grafana_admin_password` is unset in config.ini.

    A generated-once secret read straight off disk, so it can't drift from
    whatever `nyxgpt ops install` last reconciled the running Grafana
    container to (#3458).
    """
    return Path.home() / ".nyxGPT" / "secrets" / "grafana-admin-password"


def read_grafana_admin_password(cfg: ConfigParser) -> tuple[str, str]:
    """Read the already-resolvable Grafana admin password as `(password, source)`.

    The read-only half of `resolve_grafana_admin_password`'s lookup order,
    factored out so `nyxgpt ops credentials` (#3718) can *report* the
    password an operator should log in with -- and where it came from --
    without the generate-and-persist side effect. When nothing is resolvable
    yet, returns `("", "")`: printing a freshly minted secret no running
    Grafana has ever been reconciled to would be a confidently wrong answer,
    so the caller tells the operator to run `nyxgpt ops install` instead.

    `source` is a human-readable provenance string for the value returned --
    the config.ini key for a deliberate override, else the secret file's
    path.
    """
    configured = get_monitoring_grafana_admin_password(cfg).strip()
    if configured:
        return configured, "config.ini [monitoring] grafana_admin_password"

    path = grafana_admin_password_path()
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            return existing, str(path)

    return "", ""


def resolve_grafana_admin_password(cfg: ConfigParser) -> str:
    """Resolve the Grafana admin password the same way `nyxgpt ops` reconciles
    the container to it.

    `[monitoring] grafana_admin_password` wins when the user has explicitly
    set one in config.ini (a deliberate override). Otherwise this falls back
    to an ops-managed secret generated once and reused from
    `grafana_admin_password_path()` -- never the `""` fallback that
    previously guaranteed a 401 on every default-config install (#3458).

    Shared between `ops.py` (install-time reconciliation) and `health.py`
    (reading Grafana's real alert state) so both resolve the same password
    the same way instead of drifting apart (#3466). The lookup itself lives
    in `read_grafana_admin_password`; what this adds is the generate-once
    write, so only the call sites that are entitled to mint a secret do.
    """
    existing, _source = read_grafana_admin_password(cfg)
    if existing:
        return existing

    path = grafana_admin_password_path()
    password = secrets.token_urlsafe(24)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(password)
    path.chmod(0o600)
    return password


# The Slack channel nyxGPT's runtime notifications land in when
# `monitoring.slack_channel` is unset. A real default rather than "": the one
# consumer today (#3995's deferred Dedicated Host release) posts hours after
# the operator has closed the terminal, so "no channel configured" would mean
# the outcome of a billed teardown is reported to nobody.
DEFAULT_SLACK_CHANNEL = "C0ABH478QC8"


def get_monitoring_slack_webhook_url(cfg: ConfigParser) -> str:
    """Get the Slack incoming-webhook URL for Grafana's alerting contact point.

    Deliberately kept out of `get_monitoring_config` -- that dict is returned
    verbatim by `GET /api/v1/monitoring`, and this value must never be
    exposed over the API. It exists only so local tooling (`nyxgpt ops
    env-sync`/`install`) can provision Grafana's `nyxgpt-slack` contact point
    (docker/grafana/provisioning/alerting/contact-points.yml) from config.ini.

    Args:
        cfg: ConfigParser instance

    Returns:
        The configured webhook URL, or "" if unset
    """
    return cfg.get("monitoring", "slack_webhook_url", fallback="")


def get_monitoring_slack_bot_token(cfg: ConfigParser) -> str:
    """Get the Slack bot token used by CI workflows (e.g. merge-conflict notifications).

    A write-once token: Slack shows it only at creation time, so `config.ini`
    is its canonical store (#3505) and `nyxgpt ops secrets-sync` is the only
    supported way to get it into the matching `SLACK_BOT_TOKEN` GitHub
    Actions secret -- see `SECRETS_SYNC_MANIFEST`.

    Args:
        cfg: ConfigParser instance

    Returns:
        The configured bot token, or "" if unset
    """
    return cfg.get("monitoring", "slack_bot_token", fallback="")


def get_monitoring_slack_channel(cfg: ConfigParser) -> str:
    """Get the Slack channel id nyxGPT's own runtime notifications post to.

    Distinct from the CI-only channel ids beside it (`slack_huddle_channel`,
    `slack_conflict_channel`), which are pushed to GitHub Actions variables
    and read by workflows. This one is read by the *product*: the deferred
    EC2 Mac Dedicated Host release (#3995) hands it to the Step Functions
    state machine that posts the release outcome, because that message is
    delivered hours after the operator's terminal has gone.

    Defaults to `DEFAULT_SLACK_CHANNEL` rather than "" -- unlike a token,
    there is a correct default here (the owner's nyxGPT channel), and an
    empty channel would silently degrade a notification whose whole purpose
    is to arrive when nobody is watching.

    Args:
        cfg: ConfigParser instance

    Returns:
        The configured channel id, or `DEFAULT_SLACK_CHANNEL` if unset
    """
    return cfg.get("monitoring", "slack_channel", fallback="").strip() or DEFAULT_SLACK_CHANNEL


def get_openai_api_key(cfg: ConfigParser) -> str:
    """Get the OpenAI API key, human-provided and write-once at the issuing service.

    On a cloud deploy with `[secrets] provider` set, resolves from AWS SSM
    Parameter Store / Secrets Manager instead -- see `cloud_secrets.py` and
    `docs/cloud.md#cloud-secrets-ssm--secrets-manager`. Local deploys
    (`provider` unset) are unaffected.

    Returns:
        The configured key, or "" if unset
    """
    cloud_value = _resolve_cloud_secret(cfg, "openai_api_key")
    if cloud_value is not None:
        return cloud_value
    return cfg.get("openai", "api_key", fallback="")


def get_github_pat(cfg: ConfigParser) -> str:
    """Get the GitHub Personal Access Token used for repository/API operations.

    On a cloud deploy with `[secrets] provider` set, resolves from AWS SSM
    Parameter Store / Secrets Manager instead -- see `cloud_secrets.py` and
    `docs/cloud.md#cloud-secrets-ssm--secrets-manager`. Local deploys
    (`provider` unset) are unaffected.

    Returns:
        The configured PAT, or "" if unset
    """
    cloud_value = _resolve_cloud_secret(cfg, "github_pat")
    if cloud_value is not None:
        return cloud_value
    return cfg.get("github", "pat", fallback="")


def get_auth_api_key(cfg: ConfigParser) -> str:
    """Get the shared-secret API key checked by the `[auth] enabled` middleware.

    On a cloud deploy with `[secrets] provider` set, resolves from AWS SSM
    Parameter Store / Secrets Manager instead of `config.ini` -- see
    `cloud_secrets.py` and `docs/cloud.md#cloud-secrets-ssm--secrets-manager`.
    Local deploys (`provider` unset) read `[auth] api_key` exactly as before.

    Returns:
        The configured key, or "" if unset
    """
    cloud_value = _resolve_cloud_secret(cfg, "auth_api_key")
    if cloud_value is not None:
        return cloud_value
    return cfg.get("auth", "api_key", fallback="").strip()


def get_secrets_provider(cfg: ConfigParser) -> str:
    """Return the cloud secrets provider (`[secrets] provider`).

    `""` (default) means credentials come from `config.ini` as usual --
    local deploys are unaffected. Set to `"ssm"` or `"secretsmanager"` on
    cloud deploys only; see `docs/cloud.md#cloud-secrets-ssm--secrets-manager`.
    """
    return cfg.get("secrets", "provider", fallback="").strip().lower()


def get_secrets_region(cfg: ConfigParser) -> str | None:
    """Return the AWS region to resolve cloud secrets from (`[secrets] region`), or `None`.

    `None` lets boto3 fall back to its normal region resolution
    (environment variables, instance metadata, profile config).
    """
    value = cfg.get("secrets", "region", fallback="").strip()
    return value or None


def get_secrets_ssm_prefix(cfg: ConfigParser) -> str:
    """Return the SSM Parameter Store path prefix (`[secrets] ssm_prefix`).

    Falls back to `"/nyxgpt"`. Each credential is stored as an individual
    parameter at `f"{prefix}/{key}"` (e.g. `/nyxgpt/auth_api_key`).
    """
    return cfg.get("secrets", "ssm_prefix", fallback="/nyxgpt").strip() or "/nyxgpt"


def get_secrets_secretsmanager_id(cfg: ConfigParser) -> str:
    """Return the Secrets Manager secret id/ARN (`[secrets] secretsmanager_id`).

    Falls back to `"nyxgpt"`. The secret is expected to hold a single JSON
    object with one key per credential (`auth_api_key`, `openai_api_key`,
    `github_pat`).
    """
    return cfg.get("secrets", "secretsmanager_id", fallback="nyxgpt").strip() or "nyxgpt"


def _resolve_cloud_secret(cfg: ConfigParser, key: str) -> str | None:
    """Resolve `key` from the configured cloud secrets provider.

    Returns `None` when `[secrets] provider` is unset -- callers should
    read the local `config.ini` value normally in that case. Returns `""`
    (not `None`, and not the local `config.ini` value) when a provider IS
    configured but resolution fails: cloud deploys never populate these
    keys in `config.ini`, so falling back to it would just silently produce
    the same empty result while hiding a real AWS-side problem from the
    logs.
    """
    provider = get_secrets_provider(cfg)
    if not provider:
        return None
    try:
        return cloud_secrets.resolve_secret(
            provider,
            key,
            region=get_secrets_region(cfg),
            ssm_prefix=get_secrets_ssm_prefix(cfg),
            secretsmanager_id=get_secrets_secretsmanager_id(cfg),
        )
    except cloud_secrets.CloudSecretsError as exc:
        # #3837 (CodeQL #115): the exception *type* is the diagnostic; its
        # message is not. A cloud secrets provider builds that message from
        # whatever the SDK handed it, and nothing in this process constrains
        # it to be value-free -- an AWS error quoting a malformed secret
        # payload would land it in the log verbatim. The full exception is
        # still available at DEBUG for an operator who opts in on their own
        # machine; the once-per-key WARNING that everyone sees names only the
        # key, the provider and the failure class.
        _log_fallback_once(
            f"secrets.{key}",
            f"Cloud secret resolution failed for {key!r} via provider "
            f"{provider!r}: {type(exc).__name__} "
            "(enable DEBUG logging for the provider's own message)",
        )
        # #3837 (CodeQL #130). The DEBUG line above this one used to repeat
        # `provider` as a logged value, and CodeQL reads a value read out of
        # the `secrets` config section as sensitive wherever it is written to
        # a log (py/clear-text-logging-sensitive-data). That alert was opened
        # by the #115 hardening in PR #3899 -- the fix for one alert built the
        # next one. Repeating it bought nothing: the WARNING three lines up
        # already names the same key and provider once per key, and this line
        # exists for the exception, not the routing. Correlate on the key.
        _logger.debug("Cloud secret resolution failure detail for %r", key, exc_info=exc)
        return ""


def get_github_repo_owner(cfg: ConfigParser) -> str:
    """Get the GitHub repository owner `nyxgpt ops secrets-sync` pushes secrets to."""
    return cfg.get("github", "repo_owner", fallback="")


def get_github_repo_name(cfg: ConfigParser) -> str:
    """Get the GitHub repository name `nyxgpt ops secrets-sync` pushes secrets to."""
    return cfg.get("github", "repo_name", fallback="")


# The canonical-store -> GitHub Actions secrets sync manifest (#3505).
#
# `config.ini` is the single canonical store for write-once external tokens
# (Slack bot tokens, webhook URLs, PATs -- issuing services show them only
# once, so hand-editing a second copy in Actions settings lets the two drift
# silently). `nyxgpt ops secrets-sync` pushes the config.ini values declared
# here to the matching GitHub Actions secret name, one direction only
# (config.ini -> Actions); a key not listed here is never pushed, even if a
# human adds an option under the same section. Extending sync to a new
# secret is a one-line addition here -- and `tests/unit/test_sync_manifests.py`
# is what stops that addition from being forgotten: it reconciles this
# manifest against the `secrets.NAME` references the workflows actually make,
# so a secret a workflow reads and this manifest does not carry fails the
# build instead of silently becoming hand-typed state (#3976).
SECRETS_SYNC_MANIFEST: dict[str, str] = {
    "github.claude_code_oauth_token": "CLAUDE_CODE_OAUTH_TOKEN",
    "github.developer_agent_token": "DEVELOPER_AGENT_TOKEN",
    "github.scrummaster_agent_token": "SCRUMMASTER_AGENT_TOKEN",
    "github.review_agent_token": "REVIEW_AGENT_TOKEN",
    "monitoring.slack_bot_token": "SLACK_BOT_TOKEN",
    # The review huddle's three per-agent Slack user tokens (#3910). Each turn
    # posts under its own identity, so there is one token per speaker; all
    # three were typed into GitHub by hand on 2026-08-20, which is the drift
    # #3976 was filed for.
    "monitoring.slack_user_token_dev": "SLACK_USER_TOKEN_DEV",
    "monitoring.slack_user_token_review": "SLACK_USER_TOKEN_REVIEW",
    "monitoring.slack_user_token_scrum": "SLACK_USER_TOKEN_SCRUM",
    # Owner DM target for escalations (#3695) -- a Slack member id rather than
    # a credential, but it is stored as an Actions *secret*, so it is synced
    # as one and classified as one everywhere else (see the wizard override).
    "monitoring.slack_user_id": "SLACK_USER_ID",
    # Write access to the Homebrew tap the `-rc` and stable formulas are
    # pushed to (#3727).
    "homebrew.homebrew_tap_token": "HOMEBREW_TAP_TOKEN",
    # Owner-level token for the release ceremony's Phase 1 master push
    # (#3730). Unconfigured, the ceremony refuses to start.
    "github.release_ceremony_token": "RELEASE_CEREMONY_TOKEN",
}

# Deliberate omissions from `SECRETS_SYNC_MANIFEST`, recorded here because
# "not listed" and "forgotten" are indistinguishable otherwise -- which is the
# failure mode #3976 was filed for:
#
# * `github.pat`, `openai.api_key`, `auth.api_key`, `monitoring.
#   grafana_admin_password`, `error_tracking.dsn` -- credentials a *running*
#   nyxGPT instance reads. No workflow reads them; pushing them would put an
#   instance credential in CI for nothing.
# * `monitoring.slack_webhook_url` -- Grafana's `nyxgpt-slack` contact point,
#   provisioned locally by `nyxgpt ops env-sync`/`install`. No workflow reads
#   a `SLACK_WEBHOOK_URL` secret.
# * `github.myagent_review_agent_token` -- declared in example.config.ini, read
#   by nothing in `.github/workflows`.
# * `github.release_tracking_token` and `pypi.pypi_token` -- both unread in
#   this repository today (the release ceremony uses
#   `RELEASE_CEREMONY_TOKEN`; PyPI publishing uses Trusted Publishing).
#   `RELEASE_TRACKING_TOKEN`'s disposition is explicitly unsettled in ledger
#   P-004; syncing it would assert a decision nobody has made.
# * `github.qa_agent_token` and `github.gh_token_nyxagent` -- ledger **P-004**:
#   nyxAgent groundwork, deliberately not declared and not to be removed.

# The canonical-store -> GitHub Actions *variables* sync manifest (#3976).
#
# Same `section.key -> NAME` shape as `SECRETS_SYNC_MANIFEST` and the same one
# direction (config.ini -> Actions), but a different destination API and a
# very different blast radius: **repository variables are readable by anyone
# with read access to the repo**, and their values come back from the API in
# cleartext. That asymmetry is why the split between the two manifests is
# enforced structurally (`_assert_manifests_are_disjoint` below, plus
# `tests/unit/test_sync_manifests.py`) rather than defended by a comment --
# the 2026-02 predecessor of this manifest, a shell script with a 16-name
# allowlist, guarded it with a comment and was deleted without replacement.
#
# Scope: the repository variables the workflows in `.github/workflows`
# actually read as `vars.NAME`, plus the acceptance/terminal lane names the
# agent scripts read from the same source. A variable no workflow reads is not
# listed -- `DEV_AUTO_IMPLEMENT_ENABLED` is documented in
# `docs/github-tokens.md` but has no `vars.` reference, so nothing here would
# be reading what this pushed.
VARIABLES_SYNC_MANIFEST: dict[str, str] = {
    "github.agents_enabled": "AGENTS_ENABLED",
    "github.repo_owner": "REPO_OWNER",
    "github.repo_name": "REPO_NAME",
    "github.project_owner": "PROJECT_OWNER",
    "github.project_number": "PROJECT_NUMBER",
    "github.dev_agent": "DEV_AGENT",
    "github.review_agent": "REVIEW_AGENT",
    "github.scrum_agent": "SCRUM_AGENT",
    "github.human_owner": "HUMAN_OWNER",
    "github.status_field": "STATUS_FIELD",
    "github.status_backlog": "STATUS_BACKLOG",
    "github.status_in_progress": "STATUS_IN_PROGRESS",
    "github.status_in_review": "STATUS_IN_REVIEW",
    "github.status_acceptance_testing": "STATUS_ACCEPTANCE_TESTING",
    "github.status_acceptance_failed": "STATUS_ACCEPTANCE_FAILED",
    "github.status_for_release": "STATUS_FOR_RELEASE",
    "github.status_closed": "STATUS_CLOSED",
    "github.release_branch": "RELEASE_BRANCH",
    "github.release_issue_number": "RELEASE_ISSUE_NUMBER",
    "github.sprint_autopilot": "SPRINT_AUTOPILOT",
    "github.sprint_field": "SPRINT_FIELD",
    "github.drain_gate_bypass_labels": "DRAIN_GATE_BYPASS_LABELS",
    "github.agent_model_dev": "AGENT_MODEL_DEV",
    "github.agent_model_review": "AGENT_MODEL_REVIEW",
    "github.agent_model_huddle": "AGENT_MODEL_HUDDLE",
    "github.agent_model_canary": "AGENT_MODEL_CANARY",
    "github.huddle_max_rounds": "HUDDLE_MAX_ROUNDS",
    "github.review_ci_wait_minutes": "REVIEW_CI_WAIT_MINUTES",
    "github.churn_price_sheet_json": "CHURN_PRICE_SHEET_JSON",
    "homebrew.homebrew_tap_repo": "HOMEBREW_TAP_REPO",
    "monitoring.slack_huddle_channel": "SLACK_HUDDLE_CHANNEL",
    # The merge-conflict notice channel (#3911), kept separate from the huddle
    # channel above on purpose -- ledger D-041(d). Optional: blank leaves
    # `notify-merge-conflicts.yml`'s `||` fallback id in place, which is why
    # it was hand-set until #3979 gave it a config.ini key.
    "monitoring.slack_conflict_channel": "SLACK_CONFLICT_CHANNEL",
}


def _assert_manifests_are_disjoint() -> None:
    """Fail at import if a key or a name is claimed by both sync manifests (#3976).

    A repository *variable* is world-readable to anyone who can read the repo;
    a secret is not. So the one mistake this pair of manifests must make
    impossible is a credential appearing on the variables side -- by config
    key (the same `section.key` in both) or by destination name (the same
    `NAME` pushed to both APIs, where the variable would be the readable copy
    of the secret).

    Raised at import rather than checked at push time on purpose: by push time
    the value is already in memory next to an API client, and a guard that
    only runs when someone happens to run `config-sync` is not a guard. The
    test-side half of this rule (`tests/unit/test_sync_manifests.py`) is
    broader -- it derives sensitivity from the summary redactions and
    `GUIDED_SECRETS` too, which this module cannot import without a cycle.
    """
    shared_keys = sorted(set(SECRETS_SYNC_MANIFEST) & set(VARIABLES_SYNC_MANIFEST))
    if shared_keys:
        raise RuntimeError(
            "config.ini keys are in both SECRETS_SYNC_MANIFEST and "
            f"VARIABLES_SYNC_MANIFEST, so a secret would be pushed to the "
            f"world-readable variables API: {shared_keys}"
        )
    shared_names = sorted(
        set(SECRETS_SYNC_MANIFEST.values()) & set(VARIABLES_SYNC_MANIFEST.values())
    )
    if shared_names:
        raise RuntimeError(
            "the same GitHub Actions name is claimed as both a secret and a "
            f"variable: {shared_names}"
        )


_assert_manifests_are_disjoint()


def get_log_aggregation_enabled(cfg: ConfigParser) -> bool:
    """Get whether the Loki/promtail log aggregation stack is enabled.

    Disabled by default. This flag doesn't start anything by itself -- it
    only controls whether the SRE/admin dashboard treats the `logging`
    Compose profile as running and shows the log search link.

    Args:
        cfg: ConfigParser instance

    Returns:
        True if log aggregation is enabled, False otherwise
    """
    try:
        return cfg.getboolean("log_aggregation", "enabled", fallback=False)
    except Exception:
        _log_fallback_once(
            "log_aggregation.enabled",
            "Invalid log_aggregation.enabled in config, using default False",
        )
        return False


def get_log_aggregation_config(cfg: ConfigParser) -> dict:
    """Get Loki/promtail log aggregation configuration.

    Args:
        cfg: ConfigParser instance

    Returns:
        Dictionary with keys: enabled, grafana_explore_url
    """
    return {
        "enabled": get_log_aggregation_enabled(cfg),
        "grafana_explore_url": cfg.get(
            "log_aggregation",
            "grafana_explore_url",
            fallback="http://localhost:3001/explore",
        ),
    }


def get_self_heal_default_enabled(cfg: ConfigParser) -> bool:
    """Get the self-heal watchdog's initial enabled state.

    Disabled by default. Only used to seed `~/.nyxGPT/self_heal_state.json`
    on first run -- after that, the SRE/admin dashboard's toggle (persisted
    in that state file) is the source of truth, not config.ini.

    Args:
        cfg: ConfigParser instance

    Returns:
        True if the watchdog should start enabled on a fresh install
    """
    try:
        return cfg.getboolean("self_heal", "enabled", fallback=False)
    except Exception:
        _log_fallback_once(
            "self_heal.enabled", "Invalid self_heal.enabled in config, using default False"
        )
        return False


def get_self_heal_check_interval_seconds(cfg: ConfigParser) -> float:
    """Return how often the self-heal watchdog polls service health (seconds).

    Reads ``[self_heal] check_interval_seconds``, clamped to a minimum of
    1.0. Falls back to 15.0 (also used on parse errors).
    """
    try:
        return max(1.0, cfg.getfloat("self_heal", "check_interval_seconds", fallback=15.0))
    except (ValueError, TypeError):
        return 15.0


def get_self_heal_max_consecutive_restarts(cfg: ConfigParser) -> int:
    """Return the max consecutive restarts before the watchdog gives up.

    Reads ``[self_heal] max_consecutive_restarts``, clamped to a minimum
    of 1. Falls back to 5 (also used on parse errors).
    """
    try:
        return max(1, cfg.getint("self_heal", "max_consecutive_restarts", fallback=5))
    except (ValueError, TypeError):
        return 5


def get_self_heal_backoff_seconds(cfg: ConfigParser) -> float:
    """Return the backoff delay between self-heal restart attempts (seconds).

    Reads ``[self_heal] backoff_seconds``, clamped to a minimum of 0.0.
    Falls back to 30.0 (also used on parse errors).
    """
    try:
        return max(0.0, cfg.getfloat("self_heal", "backoff_seconds", fallback=30.0))
    except (ValueError, TypeError):
        return 30.0


_REDACTED = "***redacted***"


def get_effective_config_summary(cfg: ConfigParser) -> dict[str, object]:
    """Return a flat, redacted snapshot of the settings most relevant to RCA.

    Used to log what the process actually resolved each setting to at
    startup, so an operator debugging unexpected behavior doesn't have to
    guess whether a fallback silently engaged. Secrets (``error_tracking.dsn``,
    ``monitoring.grafana_admin_password``) are redacted rather than omitted,
    so their presence/absence is still visible without leaking the value.
    """
    error_tracking = get_error_tracking_config(cfg)
    return {
        "logging.level": cfg.get("logging", "level", fallback="INFO").upper(),
        "tracing.enabled": get_tracing_enabled(cfg),
        "tracing.otlp_endpoint": get_tracing_config(cfg)["otlp_endpoint"],
        "error_tracking.enabled": get_error_tracking_enabled(cfg),
        "error_tracking.dsn": _REDACTED if error_tracking["dsn"] else "",
        "monitoring.enabled": get_monitoring_enabled(cfg),
        "monitoring.grafana_admin_password": (
            _REDACTED if get_monitoring_grafana_admin_password(cfg) else ""
        ),
        "monitoring.slack_webhook_url": (
            _REDACTED if get_monitoring_slack_webhook_url(cfg) else ""
        ),
        "monitoring.slack_bot_token": (_REDACTED if get_monitoring_slack_bot_token(cfg) else ""),
        # Not redacted, and deliberately so: a channel id is not a credential,
        # and the whole reason it is summarized is that "which channel did the
        # deferred host release post to?" must be answerable from the startup
        # log rather than by re-deriving the fallback (#3995).
        "monitoring.slack_channel": get_monitoring_slack_channel(cfg),
        "openai.api_key": (_REDACTED if get_openai_api_key(cfg) else ""),
        "github.pat": (_REDACTED if get_github_pat(cfg) else ""),
        "secrets.provider": get_secrets_provider(cfg),
        "log_aggregation.enabled": get_log_aggregation_enabled(cfg),
        "rate_limit.enabled": get_rate_limit_enabled(cfg),
        "rag.enabled": get_rag_enabled(cfg),
        "batch.enabled": get_batch_enabled(cfg),
        "self_heal.enabled": get_self_heal_default_enabled(cfg),
    }


def log_effective_config(cfg: ConfigParser) -> None:
    """Log the effective (redacted) config summary once, at startup."""
    _logger.info(
        "Effective configuration",
        extra={"component": "startup", "effective_config": get_effective_config_summary(cfg)},
    )


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "load_config",
    "get_default_model",
    "get_ollama_base_url",
    "get_sessions_dir",
    "get_vectorstore_dir",
    "get_api_host",
    "get_api_port",
    "is_loopback_host",
    "get_auth_enabled",
    "validate_bind_security",
    "get_rag_enabled",
    "get_rag_chat_top_k",
    "get_rag_min_score",
    "get_rag_max_chunks",
    "get_rag_chat_context_max_chars",
    "get_rag_dedupe",
    "get_rag_include_scores",
    "get_rag_include_headers",
    "get_rag_instruction_template",
    "get_rag_context_format",
    "get_rate_limit_enabled",
    "get_rate_limit_config",
    "get_context_window_size",
    "get_context_warning_threshold",
    "get_system_prompt_minimize",
    "get_prompt_mode_enabled",
    "get_prompt_mode_short_threshold",
    "get_prompt_mode_long_threshold",
    "get_batch_enabled",
    "get_batch_size",
    "get_batch_wait_time_ms",
    "get_tracing_enabled",
    "get_tracing_config",
    "get_error_tracking_enabled",
    "get_error_tracking_config",
    "get_monitoring_enabled",
    "get_monitoring_config",
    "get_monitoring_grafana_admin_password",
    "grafana_admin_password_path",
    "read_grafana_admin_password",
    "resolve_grafana_admin_password",
    "get_monitoring_slack_webhook_url",
    "get_monitoring_slack_bot_token",
    "get_monitoring_slack_channel",
    "DEFAULT_SLACK_CHANNEL",
    "get_openai_api_key",
    "get_github_pat",
    "get_github_repo_owner",
    "get_github_repo_name",
    "get_auth_api_key",
    "get_secrets_provider",
    "get_secrets_region",
    "get_secrets_ssm_prefix",
    "get_secrets_secretsmanager_id",
    "SECRETS_SYNC_MANIFEST",
    "VARIABLES_SYNC_MANIFEST",
    "get_log_aggregation_enabled",
    "get_log_aggregation_config",
    "get_self_heal_default_enabled",
    "get_self_heal_check_interval_seconds",
    "get_self_heal_max_consecutive_restarts",
    "get_self_heal_backoff_seconds",
    "get_cassandra_pool_size",
    "get_cassandra_health_check_interval",
    "get_cassandra_reconnect_max_attempts",
    "get_cassandra_batch_size",
    "get_vector_similarity_function",
    "get_ann_oversample_factor",
    "get_cassandra_batch_query_concurrency",
    "validate_config",
    "ConfigValidationError",
    "get_effective_config_summary",
    "log_effective_config",
    "reset_fallback_warnings",
]
