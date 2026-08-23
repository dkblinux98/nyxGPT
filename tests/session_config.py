"""The config the unit suite installs for itself (#3983).

Its own module rather than a constant in `tests/conftest.py` so the standing
guard (`tests/unit/test_session_config_isolation.py`) can import it without
importing the conftest a second time -- same shape as `tests/log_guard.py`.
"""

from __future__ import annotations

# The one config the unit suite runs against, on every machine (#3983).
#
# Not "a config to fall back on when the machine has none": `_isolate_test_log_dir`
# below installs *this* text for the session even when a real
# `~/.nyxGPT/config.ini` exists, and restores the operator's file at teardown.
# The reason is that the suite's expectations are the product's *defaults*, and
# an operator config states an operator's *choices* -- so on any machine that
# actually runs the stack (the normal state for a developer) those choices
# silently became the suite's inputs. Two keys alone accounted for 134 failures
# on a clean checkout of the branch head: `[nyxgpt] session_backend = cassandra`
# routes every file-backend session/CLI/chat test at a Cassandra that isn't
# there, and the observability `enabled` flags invert every
# `*_reports_disabled_by_default` assertion. Neither is a product defect, and
# neither is visible in CI, whose runners have no operator config -- so the
# machine that runs the stack is exactly the machine that cannot trust its own
# test runs.
#
# Rewriting one key at a time (the `[tracing]` precedent below, #3415) fixes
# one report and leaves the class open; the whole file is replaced instead, so
# no future key an operator sets -- or the installer starts writing -- can
# reach the suite. `tests/unit/test_session_config_isolation.py` is the standing
# guard, per the tracing precedent.
TEST_CONFIG_TEXT = """[ollama]
base_url = http://localhost:11434

[nyxgpt]
default_model = qwen3.5:0.8b
# The session tests are about the file backend (the product default); an
# operator running the Cassandra backend must not turn them into an
# integration suite against a database this process never started (#3983).
session_backend = file

[rag]
cassandra_hosts = localhost
cassandra_port = 9042
cassandra_keyspace = nyxgpt
chat_top_k = 5
min_score = 0.0
max_chunks = 10
chunk_size = 500
chunk_overlap = 50
max_context_chars = 10000
enable_query_expansion = false
dedupe = true

[sessions]
dir = ~/.nyxGPT/sessions

[logs]
dir = ~/.nyxGPT/logs
level = INFO

[tracing]
# Tracing defaults to enabled in production (#3415), but the OTel SDK
# instrumentation `init_tracing` performs (Cassandra/urllib instrumentors, a
# global TracerProvider) is real, process-wide, and sticky across tests -- so
# the test fixture config keeps it off unless a test explicitly opts in.
enabled = false

# The remaining observability tiers, off for the same reason as tracing: each
# has a `*_reports_disabled_by_default` test asserting the safe default, and an
# operator running the monitoring/logging/errors stacks inverts all of them
# (#3983).
[monitoring]
enabled = false

[log_aggregation]
enabled = false

[error_tracking]
enabled = false

[dev]
release_branch = v1.0.0
"""
