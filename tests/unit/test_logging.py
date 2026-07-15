"""Unit tests for logging setup (issue #3181).

Regression coverage for the formatter crash that occurred when a log record
lacked the ``request_id`` attribute (e.g. records emitted by uvicorn,
cassandra-driver, or during startup before any request context exists).
"""

from __future__ import annotations

import logging
from configparser import ConfigParser
from pathlib import Path

import pytest

from nyxgpt.logging import (
    DEFAULT_FMT,
    RequestIdFilter,
    configure_logging,
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
