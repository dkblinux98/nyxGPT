"""`nyxgpt ops config-sync`: the variables half, and the one command that pushes both (#3976).

Companion to `test_ops_secrets_sync.py`. The API shapes differ in a way that
matters: a secret is `PUT` to a name-addressed endpoint and is idempotent, a
variable is `POST`ed to a collection (which 409s if the name is taken) and
then `PATCH`ed. Getting that wrong is invisible to inspection, which is why
the create/update pair is exercised against a mock transport here and against
the real API in `.github/workflows/config-sync-smoke.yml`.
"""

from __future__ import annotations

import json
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from nyxgpt import ops

pytestmark = pytest.mark.unit


def _mock_client(base_url: str, handler) -> httpx.Client:
    return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))


def _write_config(tmp_path: Path, extra: str = "") -> Path:
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[github]\n"
        "pat = ghp_" + "a" * 36 + "\n"
        "repo_owner = dkblinux98\n"
        "repo_name = nyxGPT\n" + extra
    )
    return cfg_path


# --- _variables_sync_targets: mapping enforcement ---


def test_variables_sync_targets_only_includes_mapped_keys_with_values():
    cfg = ConfigParser()
    cfg.add_section("github")
    cfg.set("github", "repo_owner", "dkblinux98")
    cfg.set("github", "status_backlog", "Backlog")
    # A secret sitting in the same section must never become a variable.
    cfg.set("github", "developer_agent_token", "ghp_should_never_be_a_variable")
    cfg.set("github", "pat", "ghp_should_never_be_a_variable_either")

    targets = ops._variables_sync_targets(cfg)

    assert {t[0] for t in targets} == {"github.repo_owner", "github.status_backlog"}
    assert {t[2] for t in targets} == {"REPO_OWNER", "STATUS_BACKLOG"}
    assert "ghp_should_never_be_a_variable" not in json.dumps(targets)


def test_variables_sync_targets_skips_blank_values():
    """Blank is how "unset" is spelled for an optional variable, not a value to push."""
    cfg = ConfigParser()
    cfg.add_section("monitoring")
    cfg.set("monitoring", "slack_huddle_channel", "   ")
    assert ops._variables_sync_targets(cfg) == []


def test_variables_sync_refuses_to_run_if_a_secret_reached_the_variables_manifest(monkeypatch):
    """Fault injection at the last point before the world-readable API.

    `config._assert_manifests_are_disjoint` already fails at import, but a
    monkeypatched or dynamically-built manifest would bypass that. This is the
    push-time half of the same rule.
    """
    from nyxgpt import config

    monkeypatch.setattr(
        config, "VARIABLES_SYNC_MANIFEST", {"monitoring.slack_bot_token": "SLACK_BOT_TOKEN_LEAK"}
    )
    cfg = ConfigParser()
    cfg.add_section("monitoring")
    cfg.set("monitoring", "slack_bot_token", "xoxb-real")

    with pytest.raises(RuntimeError, match="refusing to push secrets"):
        ops._variables_sync_targets(cfg)


# --- sync_variables_to_github_actions: guard rails ---


def test_variables_sync_missing_config_file_fails(tmp_path: Path):
    results = ops.sync_variables_to_github_actions(cfg_path=tmp_path / "missing.ini")
    assert len(results) == 1
    assert results[0].ok is False
    assert "Missing config" in results[0].message


def test_variables_sync_nothing_mapped_is_a_success_noop(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[nyxgpt]\ndefault_model = qwen2.5:0.5b\n")

    with patch.object(ops, "_github_actions_client") as mock_client:
        results = ops.sync_variables_to_github_actions(cfg_path=cfg_path)

    mock_client.assert_not_called()
    assert len(results) == 1
    assert results[0].ok is True
    assert "nothing to sync" in results[0].message.lower()


def test_variables_sync_dry_run_makes_no_network_call(tmp_path: Path):
    cfg_path = _write_config(tmp_path)

    with patch.object(ops, "_github_actions_client") as mock_client:
        results = ops.sync_variables_to_github_actions(cfg_path=cfg_path, dry_run=True)

    mock_client.assert_not_called()
    messages = " ".join(r.message for r in results)
    assert all(r.ok for r in results)
    assert "github.repo_owner -> Actions variable REPO_OWNER" in messages


def test_variables_sync_missing_pat_fails_before_any_network_call(tmp_path: Path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[github]\nrepo_owner = dkblinux98\nrepo_name = nyxGPT\n")

    with patch.object(ops, "_github_actions_client") as mock_client:
        results = ops.sync_variables_to_github_actions(cfg_path=cfg_path)

    mock_client.assert_not_called()
    assert results[0].ok is False
    assert "[github] pat" in results[0].message


# --- sync_variables_to_github_actions: mocked API ---


def test_variables_sync_creates_with_post(tmp_path: Path):
    cfg_path = _write_config(tmp_path)
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST", f"unexpected {request.method} {request.url}"
        posted.append(json.loads(request.read()))
        return httpx.Response(201)

    with patch.object(
        ops, "_github_actions_client", return_value=_mock_client(ops.GITHUB_API_BASE_URL, handler)
    ):
        results = ops.sync_variables_to_github_actions(cfg_path=cfg_path)

    assert all(r.ok for r in results)
    assert {p["name"] for p in posted} == {"REPO_OWNER", "REPO_NAME"}
    assert {p["value"] for p in posted} == {"dkblinux98", "nyxGPT"}


def test_variables_sync_updates_with_patch_when_the_name_already_exists(tmp_path: Path):
    """A 409 from the collection endpoint is "exists", not a failure.

    Every run after the first is this path, so treating the 409 as an error
    would make the command work exactly once per variable.
    """
    cfg_path = _write_config(tmp_path)
    patched = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"message": "Variable already exists"})
        assert request.method == "PATCH"
        patched.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.read())["value"]))
        return httpx.Response(204)

    with patch.object(
        ops, "_github_actions_client", return_value=_mock_client(ops.GITHUB_API_BASE_URL, handler)
    ):
        results = ops.sync_variables_to_github_actions(cfg_path=cfg_path)

    assert all(r.ok for r in results)
    assert dict(patched) == {"REPO_OWNER": "dkblinux98", "REPO_NAME": "nyxGPT"}


def test_variables_sync_reports_a_failure_per_variable_and_keeps_going(tmp_path: Path):
    cfg_path = _write_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.read())["name"] == "REPO_NAME":
            return httpx.Response(403, json={"message": "Resource not accessible"})
        return httpx.Response(201)

    with patch.object(
        ops, "_github_actions_client", return_value=_mock_client(ops.GITHUB_API_BASE_URL, handler)
    ):
        results = ops.sync_variables_to_github_actions(cfg_path=cfg_path)

    failed = [r for r in results if not r.ok]
    assert len(failed) == 1
    assert "REPO_NAME" in failed[0].message
    assert any(r.ok and "REPO_OWNER" in r.message for r in results)


# --- config_sync: the one wrapped command ---


def test_config_sync_dry_run_covers_both_destinations(tmp_path, capsys):
    """The AC: one command pushes both. A dry run is enough to prove the wiring."""
    cfg_path = _write_config(
        tmp_path, extra="[monitoring]\nslack_bot_token = xoxb-must-not-be-printed\n"
    )
    args = type("Args", (), {"config": str(cfg_path), "dry_run": True})()

    with patch.object(ops, "_github_actions_client") as mock_client:
        assert ops.config_sync(args) == 0

    mock_client.assert_not_called()
    out = capsys.readouterr().out
    assert "Actions secret SLACK_BOT_TOKEN" in out
    assert "Actions variable REPO_OWNER" in out
    assert "xoxb-must-not-be-printed" not in out


def test_config_sync_fails_if_either_half_fails(tmp_path):
    """A half-synced repository must not report success."""
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[monitoring]\nslack_bot_token = xoxb-token\n")  # no pat/repo
    args = type("Args", (), {"config": str(cfg_path), "dry_run": False})()

    with patch.object(ops, "_github_actions_client"):
        assert ops.config_sync(args) == 2


# --- config_drift: the CLI surface of the reconciliation ---


def test_config_drift_exits_zero_when_the_files_agree(tmp_path, capsys):
    example = Path(__file__).resolve().parents[2] / "example.config.ini"
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    args = type("Args", (), {"config": str(cfg_path)})()
    assert ops.config_drift(args) == 0
    assert "agree on every key" in capsys.readouterr().out


def test_config_drift_names_a_seeded_mismatch_and_exits_nonzero(tmp_path, capsys):
    """A check that always exits 0 cannot be wired into anything."""
    example = Path(__file__).resolve().parents[2] / "example.config.ini"
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        example.read_text(encoding="utf-8") + "\nseeded_undeclared_key = seeded-value\n",
        encoding="utf-8",
    )

    args = type("Args", (), {"config": str(cfg_path)})()
    assert ops.config_drift(args) == 2
    out = capsys.readouterr().out
    assert "seeded_undeclared_key" in out
    assert "seeded-value" not in out


def test_config_drift_on_a_missing_config_file_exits_nonzero(tmp_path):
    args = type("Args", (), {"config": str(tmp_path / "nope.ini")})()
    assert ops.config_drift(args) == 2
