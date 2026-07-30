"""Unit tests for logging setup (issue #3181).

Regression coverage for the formatter crash that occurred when a log record
lacked the ``request_id`` attribute (e.g. records emitted by uvicorn,
cassandra-driver, or during startup before any request context exists).
"""

from __future__ import annotations

import json
import logging
import time
from configparser import ConfigParser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from nyxgpt.logging import (
    DEFAULT_DATEFMT,
    DEFAULT_FMT,
    DEFAULT_LOGGER_NAME,
    LOG_DIR_OVERRIDE_ENV,
    AccessLogNoiseFilter,
    RequestIdFilter,
    StructuredFormatter,
    configure_logging,
    get_log_dir,
    get_logger,
    refresh_logging,
    request_id_var,
)

pytestmark = pytest.mark.unit


def _record(name: str = "some.logger", msg: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


def test_formatter_handles_record_without_request_id() -> None:
    formatter = logging.Formatter(fmt=DEFAULT_FMT, defaults={"request_id": "-"})
    record = _record()

    assert not hasattr(record, "request_id")
    formatted = formatter.format(record)

    assert "[-]" in formatted
    assert "hello" in formatted


def test_formatter_handles_record_with_request_id() -> None:
    formatter = logging.Formatter(fmt=DEFAULT_FMT, defaults={"request_id": "-"})
    record = _record()
    record.request_id = "abc-123"

    formatted = formatter.format(record)

    assert "[abc-123]" in formatted


def test_structured_formatter_includes_session_and_model_when_present() -> None:
    """StructuredFormatter should surface `session`/`model` extras when set.

    These are optional LogRecord attributes attached via `logger.info(...,
    extra={"session": ..., "model": ...})`; the formatter only emits them
    when present (hasattr checks), so a record without them must not include
    the keys.
    """
    formatter = StructuredFormatter(datefmt=DEFAULT_DATEFMT)
    record = _record()
    record.session = "session-123"
    record.model = "llama3"

    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["session"] == "session-123"
    assert data["model"] == "llama3"


def test_structured_formatter_omits_session_and_model_when_absent() -> None:
    formatter = StructuredFormatter(datefmt=DEFAULT_DATEFMT)
    record = _record()

    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert "session" not in data
    assert "model" not in data


def test_request_id_filter_injects_default_when_unset() -> None:
    filt = RequestIdFilter()
    record = _record()

    token = request_id_var.set(None)
    try:
        assert filt.filter(record) is True
        assert record.request_id == "N/A"
    finally:
        request_id_var.reset(token)


def test_request_id_filter_uses_context_var_when_set() -> None:
    filt = RequestIdFilter()
    record = _record()

    token = request_id_var.set("ctx-req-id")
    try:
        assert filt.filter(record) is True
        assert record.request_id == "ctx-req-id"
    finally:
        request_id_var.reset(token)


def test_request_id_filter_does_not_override_existing_value() -> None:
    filt = RequestIdFilter()
    record = _record()
    record.request_id = "explicit"

    token = request_id_var.set("ctx-req-id")
    try:
        filt.filter(record)
        assert record.request_id == "explicit"
    finally:
        request_id_var.reset(token)


def _make_cfg(tmp_path: Path, *, log_format: str = "text") -> ConfigParser:
    cfg = ConfigParser()
    cfg.add_section("logging")
    cfg.set("logging", "level", "INFO")
    cfg.set("logging", "dir", str(tmp_path))
    cfg.set("logging", "format", log_format)
    return cfg


@pytest.fixture(autouse=True)
def _reset_root_handlers():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_filters = root.filters[:]
    yield
    for h in root.handlers[:]:
        if h not in original_handlers:
            root.removeHandler(h)
            h.close()
    for f in root.filters[:]:
        if f not in original_filters:
            root.removeFilter(f)


def test_configure_logging_does_not_crash_for_propagated_third_party_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Records from loggers like uvicorn/cassandra-driver propagate to the
    root logger's handlers without ever passing through a Logger-level
    filter. This must not raise/emit '--- Logging error ---'."""

    configure_logging(_make_cfg(tmp_path), logger_name="nyxgpt-test-1")

    third_party_logger = logging.getLogger("uvicorn.error")
    third_party_logger.info("startup message with no request context")

    captured = capsys.readouterr()
    assert "Logging error" not in captured.err
    assert "Formatting field not found in record" not in captured.err

    log_file = tmp_path / "nyxgpt.log"
    contents = log_file.read_text()
    assert "startup message with no request context" in contents
    assert "[N/A]" in contents


def test_configure_logging_json_format_handles_missing_request_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(_make_cfg(tmp_path, log_format="json"), logger_name="nyxgpt-test-2")

    third_party_logger = logging.getLogger("cassandra.cluster")
    third_party_logger.info("driver message with no request context")

    captured = capsys.readouterr()
    assert "Logging error" not in captured.err

    log_file = tmp_path / "nyxgpt.log"
    contents = log_file.read_text()
    assert "driver message with no request context" in contents


def test_configure_logging_creates_console_handler_when_none_exists(
    tmp_path: Path,
) -> None:
    """When root has no plain StreamHandler yet, configure_logging must add one.

    _ensure_console_handler() only reuses an existing handler if it finds one;
    this exercises the "create it fresh" branch by first stripping any plain
    StreamHandlers (RotatingFileHandler is a StreamHandler subclass too, but
    is excluded) off the root logger.
    """
    root = logging.getLogger()
    removed_handlers = [
        h
        for h in root.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
    ]
    for h in removed_handlers:
        root.removeHandler(h)

    try:
        configure_logging(_make_cfg(tmp_path), logger_name="nyxgpt-test-console-new", console=True)

        stream_handlers = [
            h
            for h in root.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        ]
        assert len(stream_handlers) == 1
    finally:
        # Restore anything we stripped so we don't affect other tests/runners.
        for h in removed_handlers:
            root.addHandler(h)


def test_refresh_logging_reapplies_configuration(tmp_path: Path) -> None:
    """refresh_logging() must actually re-run configure_logging's setup.

    Verified via a real side effect (the file handler's level changing to
    match the new config), not just that some function "was called".
    """
    cfg = _make_cfg(tmp_path)
    configure_logging(cfg, logger_name="nyxgpt-test-refresh")

    log_file = tmp_path / "nyxgpt.log"
    root = logging.getLogger()

    def _our_handler() -> RotatingFileHandler:
        matches = [
            h
            for h in root.handlers
            if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == log_file
        ]
        assert len(matches) == 1
        return matches[0]

    assert _our_handler().level == logging.INFO

    cfg.set("logging", "level", "DEBUG")
    refresh_logging(cfg)

    assert _our_handler().level == logging.DEBUG


def test_configure_logging_formats_timestamps_in_utc(tmp_path: Path) -> None:
    """Promtail's `timestamp` pipeline stage parses log lines as UTC (#3349)
    -- the formatter must actually emit UTC-zoned timestamps, not host-local
    time, or every parsed line shifts by the host's UTC offset and lands
    outside the curated Explore links' now-1h window."""
    configure_logging(_make_cfg(tmp_path), logger_name="nyxgpt-test-utc")

    root = logging.getLogger()
    log_file = tmp_path / "nyxgpt.log"
    fh = next(
        h
        for h in root.handlers
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == log_file
    )

    record = _record()
    record.created = 1700000000.0  # a fixed, known epoch

    formatted_time = fh.formatter.formatTime(record, DEFAULT_DATEFMT)
    expected = time.strftime(DEFAULT_DATEFMT, time.gmtime(1700000000.0))
    assert formatted_time == expected


def test_configure_logging_json_format_also_uses_utc(tmp_path: Path) -> None:
    configure_logging(_make_cfg(tmp_path, log_format="json"), logger_name="nyxgpt-test-utc-json")

    root = logging.getLogger()
    log_file = tmp_path / "nyxgpt.log"
    fh = next(
        h
        for h in root.handlers
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == log_file
    )

    record = _record()
    record.created = 1700000000.0

    formatted_time = fh.formatter.formatTime(record, DEFAULT_DATEFMT)
    expected = time.strftime(DEFAULT_DATEFMT, time.gmtime(1700000000.0))
    assert formatted_time == expected


def test_get_logger_defaults_to_app_logger_name() -> None:
    logger = get_logger()
    assert logger.name == DEFAULT_LOGGER_NAME


def test_get_logger_returns_named_logger() -> None:
    logger = get_logger("nyxgpt.custom")
    assert logger.name == "nyxgpt.custom"


def _access_record(path: str, status: int = 200) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:12345", "GET", path, "1.1", status),
        exc_info=None,
    )


def test_access_log_noise_filter_drops_health_and_metrics() -> None:
    f = AccessLogNoiseFilter()
    assert f.filter(_access_record("/health")) is False
    assert f.filter(_access_record("/metrics")) is False
    assert f.filter(_access_record("/metrics?foo=bar")) is False


def test_access_log_noise_filter_keeps_other_paths() -> None:
    f = AccessLogNoiseFilter()
    assert f.filter(_access_record("/api/v1/chat/stream")) is True
    assert f.filter(_access_record("/healthcheck-typo")) is False  # prefix match, by design


def test_access_log_noise_filter_tolerates_missing_args() -> None:
    f = AccessLogNoiseFilter()
    record = _record()  # generic record with no positional args tuple content
    assert f.filter(record) is True


def test_configure_logging_attaches_access_log_noise_filter_once(tmp_path: Path) -> None:
    configure_logging(_make_cfg(tmp_path), logger_name="nyxgpt-test-access-filter")
    configure_logging(_make_cfg(tmp_path), logger_name="nyxgpt-test-access-filter")

    access_logger = logging.getLogger("uvicorn.access")
    filters = [f for f in access_logger.filters if isinstance(f, AccessLogNoiseFilter)]
    assert len(filters) == 1


def test_get_log_dir_prefers_explicit_cfg_dir_over_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cfg that explicitly sets [logging] dir always wins (see #3443) --
    otherwise tests like this one that build their own tmp_path config would
    break under the test-suite-wide NYXGPT_LOG_DIR override."""
    explicit_dir = tmp_path / "cfg-dir"
    cfg = _make_cfg(explicit_dir)
    monkeypatch.setenv(LOG_DIR_OVERRIDE_ENV, str(tmp_path / "env-dir"))

    assert get_log_dir(cfg) == explicit_dir


def test_get_log_dir_falls_back_to_env_override_when_cfg_omits_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When [logging] dir isn't set at all (e.g. a bare/isolated test config),
    NYXGPT_LOG_DIR is used instead of the hardcoded ~/.nyxGPT/logs default
    (see #3443) -- this is what protects tests that swap in their own minimal
    config with no logging section."""
    cfg = ConfigParser()
    env_dir = tmp_path / "env-dir"
    monkeypatch.setenv(LOG_DIR_OVERRIDE_ENV, str(env_dir))

    assert get_log_dir(cfg) == env_dir


def test_get_log_dir_default_is_unchanged_without_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The test suite itself sets NYXGPT_LOG_DIR session-wide (see
    # tests/conftest.py); unset it here to exercise the true default.
    monkeypatch.delenv(LOG_DIR_OVERRIDE_ENV, raising=False)
    cfg = ConfigParser()

    assert get_log_dir(cfg) == Path.home() / ".nyxGPT" / "logs"
