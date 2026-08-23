"""Install-time bootstrap of the models a first chat needs (#3824).

`nyxgpt ops install` used to report every service healthy on a machine whose
Ollama had never downloaded a model: the web UI loaded, `ops status` was clean,
and the user's first chat message failed. These tests pin the behavior that
replaced it -- both configured models pulled at install time, on every run
mode, read from configuration, idempotent, and a failure that fails the
install rather than being reported as success.
"""

from __future__ import annotations

import json
import logging
from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from nyxgpt import model_bootstrap, ops
from nyxgpt.config import get_default_model


def _cfg(**overrides: str) -> ConfigParser:
    cfg = ConfigParser()
    cfg["nyxgpt"] = {"default_model": overrides.get("default_model", "qwen3:0.6b")}
    cfg["ollama"] = {"base_url": "http://127.0.0.1:11434"}
    cfg["rag"] = {"embedding_model": overrides.get("embedding_model", "nomic-embed-text")}
    return cfg


def _no_config_anywhere(monkeypatch, tmp_path) -> None:
    """Make "this machine has never been configured" true for real.

    Redirecting `Path.home()` alone is not enough to reproduce it: the
    developer's own `~/.nyxGPT/config.ini` is already baked into
    `config.DEFAULT_CONFIG_PATH` at import, so a fallback that reaches for the
    default *path* silently reads it and the test passes on a machine that is
    not bare. Both have to be pointed at nothing (#3775: inject the condition).
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr("nyxgpt.config.DEFAULT_CONFIG_PATH", tmp_path / "absent" / "config.ini")
    assert not (tmp_path / ".nyxGPT" / "config.ini").exists()


def _installed(monkeypatch, names: list[str]) -> None:
    monkeypatch.setattr(
        model_bootstrap,
        "installed_model_names",
        lambda base_url=None: {model_bootstrap.normalize_model_name(n) for n in names},
    )


def _record_pulls(monkeypatch) -> list[tuple[str, str, float]]:
    pulled: list[tuple[str, str, float]] = []

    def fake_pull(name, base_url=None, progress_callback=None, timeout_s=600.0):  # noqa: ARG001
        pulled.append((name, base_url, timeout_s))
        return {"status": "success"}

    monkeypatch.setattr("nyxgpt.models.pull_model", fake_pull)
    return pulled


# ---------------------------------------------------------------------------
# Which models are required
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_both_models_are_required_regardless_of_the_rag_toggle():
    """RAG is a per-session toggle, so "RAG is off" never means "skip it"."""
    cfg = _cfg()
    cfg["rag"]["enable_chat_context"] = "false"
    roles = {m.role: m.name for m in model_bootstrap.required_models(cfg)}
    assert roles == {"chat": "qwen3:0.6b", "embedding": "nomic-embed-text"}


@pytest.mark.unit
def test_required_models_come_from_config_not_literals():
    cfg = _cfg(default_model="llama3.1:8b", embedding_model="mxbai-embed-large")
    assert [m.name for m in model_bootstrap.required_models(cfg)] == [
        "llama3.1:8b",
        "mxbai-embed-large",
    ]


@pytest.mark.unit
def test_blank_embedding_model_falls_back_to_the_chat_model_and_is_listed_once():
    """`get_embedding_config` resolves a blank `[rag] embedding_model` to the
    chat model; the bootstrap must resolve it identically, and not ask Ollama
    for the same model twice."""
    cfg = _cfg(embedding_model="")
    assert [(m.role, m.name) for m in model_bootstrap.required_models(cfg)] == [
        ("chat", "qwen3:0.6b")
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("nomic-embed-text", "nomic-embed-text:latest"),
        ("qwen3:0.6b", "qwen3:0.6b"),
        ("registry.example.com:5000/team/model", "registry.example.com:5000/team/model:latest"),
    ],
)
def test_untagged_names_normalize_to_ollamas_latest_tag(configured: str, expected: str):
    """`/api/tags` reports `nomic-embed-text` as `nomic-embed-text:latest`; without
    this the bootstrap would re-pull a present model on every single install."""
    assert model_bootstrap.normalize_model_name(configured) == expected


# ---------------------------------------------------------------------------
# Pulling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_models_are_pulled(monkeypatch):
    _installed(monkeypatch, [])
    pulled = _record_pulls(monkeypatch)

    outcomes = model_bootstrap.ensure_required_models(cfg=_cfg())

    assert [name for name, _url, _t in pulled] == ["qwen3:0.6b", "nomic-embed-text"]
    assert all(o.ok and not o.already_present for o in outcomes)


@pytest.mark.unit
def test_the_pull_is_idempotent(monkeypatch):
    """A re-install over a warm machine must download nothing."""
    _installed(monkeypatch, ["qwen3:0.6b", "nomic-embed-text"])

    def fail_pull(*_a, **_k):
        raise AssertionError("nothing may be downloaded when both models are present")

    monkeypatch.setattr("nyxgpt.models.pull_model", fail_pull)

    outcomes = model_bootstrap.ensure_required_models(cfg=_cfg())

    assert [o.already_present for o in outcomes] == [True, True]
    assert all(o.ok for o in outcomes)


@pytest.mark.unit
def test_only_the_missing_model_is_pulled(monkeypatch):
    _installed(monkeypatch, ["qwen3:0.6b"])
    pulled = _record_pulls(monkeypatch)

    model_bootstrap.ensure_required_models(cfg=_cfg())

    assert [name for name, _url, _t in pulled] == ["nomic-embed-text"]


@pytest.mark.unit
def test_a_failed_pull_is_reported_not_raised(monkeypatch):
    _installed(monkeypatch, [])

    def broken_pull(*_a, **_k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr("nyxgpt.models.pull_model", broken_pull)

    outcomes = model_bootstrap.ensure_required_models(cfg=_cfg())

    assert [o.ok for o in outcomes] == [False, False]
    assert "connection reset" in outcomes[0].detail
    assert "[nyxgpt] default_model" in outcomes[0].detail


@pytest.mark.unit
def test_an_unreachable_ollama_is_a_failure_for_every_model(monkeypatch):
    def unreachable(base_url=None):  # noqa: ARG001
        raise RuntimeError("connection refused")

    monkeypatch.setattr(model_bootstrap, "installed_model_names", unreachable)

    outcomes = model_bootstrap.ensure_required_models(cfg=_cfg())

    assert [o.ok for o in outcomes] == [False, False]
    assert "Could not list models" in outcomes[0].detail


@pytest.mark.unit
def test_missing_required_models_propagates_unreachability(monkeypatch):
    """ "Cannot tell" must never be reported as "nothing is missing"."""

    def unreachable(base_url=None):  # noqa: ARG001
        raise RuntimeError("connection refused")

    monkeypatch.setattr(model_bootstrap, "installed_model_names", unreachable)

    with pytest.raises(RuntimeError):
        model_bootstrap.missing_required_models(cfg=_cfg())


@pytest.mark.unit
def test_the_remediation_names_nyxgpt_commands_only():
    """Operational Command Wrapping: no raw `ollama pull` in user-facing text."""
    hint = model_bootstrap.missing_models_hint(model_bootstrap.required_models(_cfg()))
    assert "nyxgpt ops install" in hint
    assert "nyxgpt models pull qwen3:0.6b" in hint
    assert "ollama pull" not in hint


# ---------------------------------------------------------------------------
# The install step
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_install_step_fails_when_a_model_could_not_be_pulled(monkeypatch):
    """The whole point: `ops install` must not report success with chat broken."""
    monkeypatch.setattr(
        model_bootstrap,
        "ensure_required_models",
        lambda **_k: [
            model_bootstrap.ModelPullOutcome(
                model=model_bootstrap.RequiredModel("chat", "qwen3:0.6b", "[nyxgpt] default_model"),
                ok=False,
                already_present=False,
                detail="registry timed out",
            )
        ],
    )

    results = ops._ensure_required_models()

    assert [r.ok for r in results] == [False]
    assert "qwen3:0.6b" in results[0].message
    assert "registry timed out" in results[0].details


@pytest.mark.unit
def test_install_step_reports_present_models_as_ok(monkeypatch):
    monkeypatch.setattr(
        model_bootstrap,
        "ensure_required_models",
        lambda **_k: [
            model_bootstrap.ModelPullOutcome(
                model=model_bootstrap.RequiredModel(role, name, setting),
                ok=True,
                already_present=True,
                detail=f"'{name}' already installed",
            )
            for role, name, setting in (
                ("chat", "qwen3:0.6b", "[nyxgpt] default_model"),
                ("embedding", "nomic-embed-text", "[rag] embedding_model"),
            )
        ],
    )

    results = ops._ensure_required_models()

    assert [r.ok for r in results] == [True, True]


@pytest.mark.unit
def test_install_runs_the_model_step_on_every_native_path():
    """No flag may skip the pull -- an install that reports success while chat
    is broken is exactly the state this issue exists to eliminate."""
    import inspect

    source = inspect.getsource(ops.install)
    assert '("required models", _ensure_required_models)' in source
    # ...and it is not inside the `--skip-observability` conditional.
    assert source.index('("required models"') < source.index("skip_observability")


@pytest.mark.unit
def test_terraform_install_pulls_the_same_models():
    import inspect

    source = inspect.getsource(ops._install_terraform_steps)
    assert '("required models", _ensure_required_models)' in source


# ---------------------------------------------------------------------------
# status / doctor reporting
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_reports_each_model_and_the_fix(monkeypatch, capsys):
    _installed(monkeypatch, ["qwen3:0.6b"])
    monkeypatch.setattr("nyxgpt.config.load_config", lambda *_a, **_k: _cfg())

    info = ops.required_models_status(cfg=_cfg())
    ops._print_required_models_status()

    assert info["ready"] is False
    assert [m["present"] for m in info["models"]] == [True, False]
    out = capsys.readouterr().out
    assert "chat: qwen3:0.6b -- PRESENT" in out
    assert "embedding: nomic-embed-text -- MISSING" in out
    assert "nyxgpt ops install" in out


@pytest.mark.unit
def test_status_says_unknown_rather_than_missing_when_ollama_is_down(monkeypatch):
    def unreachable(base_url=None):  # noqa: ARG001
        raise RuntimeError("connection refused")

    monkeypatch.setattr(model_bootstrap, "installed_model_names", unreachable)

    info = ops.required_models_status(cfg=_cfg())

    assert info["reachable"] is False
    assert [m["present"] for m in info["models"]] == [None, None]
    assert info["ready"] is False


@pytest.mark.unit
def test_status_error_names_the_failure_class_not_the_transport_message(monkeypatch, caplog):
    """#3837 (CodeQL #129): this dict is served verbatim by `GET /models/required`.

    Same fault class as #123 — a caught exception's *message* stored into a
    structure an endpoint returns. Here the `except Exception` catches whatever
    httpx raises against an unreachable Ollama, and that string names the base
    URL's resolution failure and the host's proxy. The class is what the
    dashboard renders; the message belongs in the log.
    """
    host_state = "proxy.corp.internal:3128 (resolver ns1.corp.internal)"

    def unreachable(base_url=None):  # noqa: ARG001
        raise RuntimeError(f"connection refused via {host_state}")

    monkeypatch.setattr(model_bootstrap, "installed_model_names", unreachable)

    with caplog.at_level(logging.WARNING, logger="nyxgpt.ops"):
        info = ops.required_models_status(cfg=_cfg())

    assert host_state not in json.dumps(info)
    # Still diagnostic, and still reported somewhere the operator can reach.
    assert info["error"] == "RuntimeError"
    assert host_state in caplog.text


@pytest.mark.unit
def test_doctor_reports_a_missing_model(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[nyxgpt]\ndefault_model = qwen3:0.6b\n\n[rag]\nembedding_model = nomic-embed-text\n",
        encoding="utf-8",
    )
    _installed(monkeypatch, ["qwen3:0.6b"])

    issue = ops._missing_required_models_issue(cfg_path)

    assert issue is not None
    assert "nomic-embed-text" in issue
    assert "nyxgpt ops install" in issue


@pytest.mark.unit
def test_doctor_stays_silent_when_ollama_is_unreachable(monkeypatch, tmp_path):
    """That is the ollama service's failure, reported elsewhere -- guessing
    "model missing" from it would misname the fault."""
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[nyxgpt]\ndefault_model = qwen3:0.6b\n", encoding="utf-8")

    def unreachable(base_url=None):  # noqa: ARG001
        raise RuntimeError("connection refused")

    monkeypatch.setattr(model_bootstrap, "installed_model_names", unreachable)

    assert ops._missing_required_models_issue(cfg_path) is None


@pytest.mark.unit
def test_status_reports_defaults_instead_of_raising_when_there_is_no_config(
    monkeypatch, tmp_path, capsys
):
    """`ops status` is the diagnostic a user runs on a machine that has not been
    configured yet, and its contract is to always return 0.

    The first cut asked `load_config` for the defaults by passing None -- but
    None means "the default *path*", so the no-config case landed in the very
    FileNotFoundError the `exists()` guard had just detected, and `ops status`
    died with a traceback before printing its model block. Every other test
    here hands in a cfg or patches `load_config`, so nothing covered the branch
    that actually runs on a bare machine.
    """
    _no_config_anywhere(monkeypatch, tmp_path)
    _installed(monkeypatch, [])

    info = ops.required_models_status()
    ops._print_required_models_status()

    assert info["reachable"] is True
    assert info["ready"] is False
    # The code defaults, reported as missing -- what an unconfigured machine
    # would in fact ask Ollama for. Asked of `get_default_model` rather than
    # spelled out: this test is about the no-config branch not raising, and
    # hard-coding the model name made it fail on every run from the day the
    # default changed (`llama3.1:8b` -> `qwen3.5:0.8b`, commit 1ece87b0) for a
    # reason that has nothing to do with what it tests (#4020).
    code_default = get_default_model(ConfigParser())
    assert [m["role"] for m in info["models"]] == ["chat"]
    assert info["models"][0]["model"] == code_default
    assert info["models"][0]["present"] is False
    out = capsys.readouterr().out
    assert f"chat: {code_default} -- MISSING" in out
    assert "nyxgpt ops install" in out


@pytest.mark.unit
def test_ops_status_survives_a_machine_with_no_config(monkeypatch, tmp_path):
    """The whole command, not just the helper: `ops status` returns 0 rather
    than raising on a bare machine (the k8s pod-state smoke calls it there)."""
    _no_config_anywhere(monkeypatch, tmp_path)
    _installed(monkeypatch, [])

    assert ops.status(SimpleNamespace()) == 0


@pytest.mark.unit
def test_status_says_none_configured_only_for_an_explicitly_empty_model(
    monkeypatch, tmp_path, capsys
):
    """The "none configured" line is reachable exactly one way: a config.ini
    that sets the model keys to the empty string. An *absent* key falls back to
    the code default instead, which is why this is not the no-config message."""
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir()
    (cfg_dir / "config.ini").write_text(
        "[nyxgpt]\ndefault_model =\n\n[rag]\nembedding_model =\n", encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    _installed(monkeypatch, [])

    info = ops.required_models_status()
    ops._print_required_models_status()

    assert info["models"] == []
    assert "none configured" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Compose: the models the container-run mode pre-pulls
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_env_sync_derives_the_compose_model_vars_from_config(tmp_path, monkeypatch):
    """docker-compose.yml's ollama service reads these to know what to pull, so
    they must follow config.ini rather than being literals in the compose file."""
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[nyxgpt]\ndefault_model = llama3.1:8b\n\n"
        "[rag]\nembedding_model = mxbai-embed-large\n\n"
        "[auth]\nenabled = true\napi_key = secret\n",
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"

    results = ops.sync_env_from_config(cfg_path=cfg_path, env_path=env_path)

    assert all(r.ok for r in results)
    env = dict(
        line.split("=", 1) for line in env_path.read_text(encoding="utf-8").splitlines() if line
    )
    assert env["NYXGPT_DEFAULT_MODEL"] == "llama3.1:8b"
    assert env["NYXGPT_EMBEDDING_MODEL"] == "mxbai-embed-large"


@pytest.mark.unit
def test_env_sync_writes_the_model_vars_even_with_no_secrets(tmp_path):
    """A localhost-only install has no api key to sync; the Compose stack still
    needs to know which models to pull."""
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[nyxgpt]\ndefault_model = qwen3:0.6b\n\n[rag]\nembedding_model = nomic-embed-text\n\n"
        "[auth]\nenabled = false\n",
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"

    results = ops.sync_env_from_config(cfg_path=cfg_path, env_path=env_path)

    assert all(r.ok for r in results)
    text = env_path.read_text(encoding="utf-8")
    assert "NYXGPT_DEFAULT_MODEL=qwen3:0.6b" in text
    assert "NYXGPT_EMBEDDING_MODEL=nomic-embed-text" in text
