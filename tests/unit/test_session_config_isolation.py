"""Standing guard: the unit suite runs against its own config, never the machine's (#3983).

`_isolate_test_log_dir` in `tests/conftest.py` installs `TEST_CONFIG_TEXT` as
`~/.nyxGPT/config.ini` for the session and restores the operator's file at
teardown. Without it, a developer machine that actually runs the stack -- the
normal state -- feeds its own operator choices to the suite: a full
`pytest tests/unit/` on the v3.0.0 head failed 134 tests purely from
`[nyxgpt] session_backend = cassandra` plus the observability `enabled` flags,
while the same tree passed in CI, whose runners have no operator config.

None of those 134 reports named the cause: they surfaced as connection
refused, or as a `*_reports_disabled_by_default` test seeing "enabled". These
assertions fail *with the cause* instead, one per key the isolation is
load-bearing for -- the same role `test_session_config_keeps_tracing_disabled`
(tests/unit/test_tracing.py) plays for the tracing half, which this file
generalizes rather than replaces.
"""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

import pytest
from session_config import TEST_CONFIG_TEXT

from nyxgpt.config import (
    get_error_tracking_enabled,
    get_log_aggregation_enabled,
    get_monitoring_enabled,
    get_session_backend,
    get_tracing_enabled,
    load_config,
)


@pytest.mark.unit
def test_session_config_keeps_the_file_session_backend() -> None:
    """The session/CLI/chat tests are written against the file backend.

    An operator running `session_backend = cassandra` otherwise turns them into
    an integration suite against a database no test started: 122 of the 134
    leaked failures were one `SessionStoreError: Cannot reach Cassandra for the
    session store`.
    """
    assert get_session_backend(load_config(None)) == "file"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "reader"),
    [
        ("tracing", get_tracing_enabled),
        ("monitoring", get_monitoring_enabled),
        ("log_aggregation", get_log_aggregation_enabled),
        ("error_tracking", get_error_tracking_enabled),
    ],
)
def test_session_config_keeps_observability_disabled(name, reader) -> None:
    """Each tier has a `*_reports_disabled_by_default` endpoint test.

    Those assert the product's safe default; an operator with the stack
    running inverts every one of them, and the failure reads as if the
    endpoint were wrong.
    """
    assert reader(load_config(None)) is False, (
        f"[{name}] enabled leaked into the session config -- the suite is "
        "reading the machine's operator config, not TEST_CONFIG_TEXT"
    )


@pytest.mark.unit
def test_session_config_carries_no_keys_beyond_the_test_config() -> None:
    """The generalization of the two guards above.

    They name the keys that have already cost a red suite; this one fails for
    the next such key before anyone has to diagnose it, by asserting the
    *whole* config on disk is the suite's own. `[logging] dir` is the one
    documented addition (the per-session temp log dir the same fixture
    redirects to, #3443).

    Read from the file rather than from `load_config(None)`: that call returns
    a process-wide cached `ConfigParser` which product code legitimately
    mutates in place (`configure_logging` adds `[logging] level`), and this
    guard is about what the suite *installed*, not about what a later caller
    derived from it.
    """
    expected = ConfigParser()
    expected.read_string(TEST_CONFIG_TEXT)

    actual = ConfigParser()
    actual.read(Path.home() / ".nyxGPT" / "config.ini", encoding="utf-8")

    extra = {
        f"[{section}] {key}"
        for section in actual.sections()
        for key in actual.options(section)
        if not expected.has_option(section, key) and (section, key) != ("logging", "dir")
    }
    assert not extra, (
        f"config keys the suite did not install are in effect: {sorted(extra)} -- "
        "the session-scoped config isolation in tests/conftest.py is not covering "
        "the machine's ~/.nyxGPT/config.ini"
    )
