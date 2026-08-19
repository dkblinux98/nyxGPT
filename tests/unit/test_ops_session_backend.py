"""Unit tests for `nyxgpt ops session-backend` (#3865).

The wrapped replacement for SSHing to a deployed instance and hand-editing
`[nyxgpt] session_backend` in config.ini. Nothing here touches a real
`~/.nyxGPT`: every test drives an explicit `--config` path under tmp_path.
"""

import argparse

import pytest

from nyxgpt import ops

# A config.ini shaped like the one the provisioning scripts seed from
# `example.config.ini`: the key is present, set to the back-compat default,
# and surrounded by the comments that document it.
SEEDED_CONFIG = """\
[nyxgpt]
# Where chat sessions are stored.
# Can be overridden with the NYXGPT_SESSION_BACKEND environment variable.
session_backend = file

# Where chat sessions are stored with the "file" backend.
sessions_dir = ~/.nyxGPT/sessions

[rag]
cassandra_hosts = localhost
"""


@pytest.fixture()
def config_path(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text(SEEDED_CONFIG, encoding="utf-8")
    return path


def _args(**overrides) -> argparse.Namespace:
    namespace = argparse.Namespace(backend=None, config=None)
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


# --- The writer ---------------------------------------------------------


def test_setting_cassandra_rewrites_only_the_one_line(config_path):
    results = ops.set_session_backend("cassandra", cfg_path=config_path)

    assert all(r.ok for r in results)
    text = config_path.read_text(encoding="utf-8")
    assert "session_backend = cassandra" in text
    # The whole reason this is line-based rather than a ConfigParser
    # round-trip: config.ini is the seeded example, and its comments are the
    # only documentation an operator on the instance has.
    assert "# Can be overridden with the NYXGPT_SESSION_BACKEND" in text
    assert "sessions_dir = ~/.nyxGPT/sessions" in text
    assert "cassandra_hosts = localhost" in text


def test_setting_the_value_already_in_place_writes_nothing(config_path):
    ops.set_session_backend("cassandra", cfg_path=config_path)
    before = config_path.read_text(encoding="utf-8")
    mtime = config_path.stat().st_mtime_ns

    results = ops.set_session_backend("cassandra", cfg_path=config_path)

    assert all(r.ok for r in results)
    assert "already" in results[0].message
    assert config_path.read_text(encoding="utf-8") == before
    # Idempotence is what makes it safe for a re-deploy to call this every run.
    assert config_path.stat().st_mtime_ns == mtime


def test_switching_back_to_file_is_supported(config_path):
    ops.set_session_backend("cassandra", cfg_path=config_path)

    results = ops.set_session_backend("file", cfg_path=config_path)

    assert all(r.ok for r in results)
    assert "session_backend = file" in config_path.read_text(encoding="utf-8")


def test_a_config_without_the_key_gets_it_added(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("[nyxgpt]\ndefault_model = llama3\n", encoding="utf-8")

    results = ops.set_session_backend("cassandra", cfg_path=path)

    assert all(r.ok for r in results)
    text = path.read_text(encoding="utf-8")
    assert "session_backend = cassandra" in text
    assert "default_model = llama3" in text


def test_an_unknown_backend_is_refused_without_writing(config_path):
    before = config_path.read_text(encoding="utf-8")

    results = ops.set_session_backend("postgres", cfg_path=config_path)

    assert not any(r.ok for r in results)
    # Writing it would be worse than refusing: `get_session_backend` warns and
    # silently falls back to `file`, which is exactly the silent wrong-store
    # failure this whole command exists to stop.
    assert config_path.read_text(encoding="utf-8") == before


def test_a_missing_config_names_the_command_that_creates_one(tmp_path):
    results = ops.set_session_backend("cassandra", cfg_path=tmp_path / "absent.ini")

    assert not any(r.ok for r in results)
    assert "wizard" in results[0].details


def test_the_written_config_keeps_its_0600(config_path):
    config_path.chmod(0o600)

    ops.set_session_backend("cassandra", cfg_path=config_path)

    assert config_path.stat().st_mode & 0o777 == 0o600


# --- The CLI entrypoint -------------------------------------------------


def test_no_argument_reports_the_backend_in_force_and_changes_nothing(config_path, capsys):
    before = config_path.read_text(encoding="utf-8")

    code = ops.session_backend(_args(config=str(config_path)))

    assert code == 0
    out = capsys.readouterr().out
    assert "session_backend = file" in out
    assert str(config_path) in out
    # A read, which is what puts it in `cloud_deploy.REMOTE_OPS_COMMANDS`.
    assert config_path.read_text(encoding="utf-8") == before


def test_the_reader_reports_the_env_override_rather_than_the_file(config_path, capsys, monkeypatch):
    ops.set_session_backend("cassandra", cfg_path=config_path)
    monkeypatch.setenv("NYXGPT_SESSION_BACKEND", "file")

    code = ops.session_backend(_args(config=str(config_path)))

    assert code == 0
    out = capsys.readouterr().out
    # config.ini says cassandra; the process would actually use file. An
    # operator debugging a container that disagrees with its config needs the
    # value in force, not the value on disk.
    assert "session_backend = file" in out
    assert "NYXGPT_SESSION_BACKEND" in out


def test_setting_via_the_cli_returns_zero_and_writes(config_path, capsys):
    code = ops.session_backend(_args(backend="cassandra", config=str(config_path)))

    assert code == 0
    assert "session_backend = cassandra" in config_path.read_text(encoding="utf-8")
    assert "[OK]" in capsys.readouterr().out


def test_a_failed_set_returns_two(tmp_path, capsys):
    code = ops.session_backend(_args(backend="cassandra", config=str(tmp_path / "absent.ini")))

    assert code == 2
    assert "[FAIL]" in capsys.readouterr().out
