"""The shipped chat model is stated once, and everything else reads it.

`1ece87b0` changed the model in three of nine files. A Kubernetes install then
served a different model than a native one, `ops status` reported a third, and
it took an eleven-PR pile-up to notice. The earlier remedy -- a guard asserting
the nine copies agree -- manages the symptom; the owner's decision (2026-08-23)
was to remove the copies instead.

These tests pin the *property* (each site reads) rather than a value, so they
cannot go stale the way a restated constant does.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest

from nyxgpt import ops
from nyxgpt.config import get_default_model, shipped_default_model
from nyxgpt.wizard import SHIPPED_DEFAULT_MODEL

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_shipped_default_is_read_from_the_packaged_file() -> None:
    """Not a literal in config.py: the packaged example.config.ini is the source."""
    shipped = configparser.ConfigParser()
    shipped.read(REPO_ROOT / "example.config.ini", encoding="utf-8")
    assert shipped_default_model() == shipped.get("nyxgpt", "default_model").strip()


def test_config_py_does_not_restate_the_model() -> None:
    """A literal fallback here is a second answer to 'which model ships?'."""
    src = (REPO_ROOT / "src" / "nyxgpt" / "config.py").read_text(encoding="utf-8")
    restated = re.findall(r'"nyxgpt",\s*"default_model",\s*fallback="([^"]+)"', src)
    assert not restated, f"config.py restates a shipped model: {restated}"


def test_the_wizard_agrees_because_it_reads_rather_than_restates() -> None:
    src = (REPO_ROOT / "src" / "nyxgpt" / "wizard.py").read_text(encoding="utf-8")
    assert (
        "SHIPPED_DEFAULT_MODEL = shipped_default_model()" in src
    ), "wizard.py should read the shipped model, not declare one"
    assert shipped_default_model() == SHIPPED_DEFAULT_MODEL


def test_an_operators_configured_model_wins_over_the_shipped_one() -> None:
    cfg = configparser.ConfigParser()
    cfg.add_section("nyxgpt")
    cfg.set("nyxgpt", "default_model", "operators-choice:9b")
    assert get_default_model(cfg) == "operators-choice:9b"


def test_the_k8s_configmap_is_rendered_from_config_not_shipped_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ConfigMap is the cluster's config.ini; it must not carry its own copy."""
    (tmp_path / "k8s").mkdir()
    target = tmp_path / "k8s" / "configmap.yaml"
    target.write_text(
        "data:\n  config.ini: |\n    [nyxgpt]\n    default_model = SHIPPED\n"
        "    [rag]\n    embedding_model = SHIPPED-EMB\n",
        encoding="utf-8",
    )

    cfg = configparser.ConfigParser()
    cfg.add_section("nyxgpt")
    cfg.set("nyxgpt", "default_model", "operators-choice:9b")
    cfg.add_section("rag")
    cfg.set("rag", "embedding_model", "operators-emb")
    monkeypatch.setattr(ops, "NYXGPT_HOME", tmp_path)
    monkeypatch.setattr(ops, "load_config", lambda *_a, **_k: cfg)

    ops._render_k8s_config_models()

    rendered = target.read_text(encoding="utf-8")
    assert "default_model = operators-choice:9b" in rendered
    assert "embedding_model = operators-emb" in rendered
    assert "SHIPPED" not in rendered


def test_rendering_never_fails_an_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable config leaves the manifest as shipped -- it does not raise."""
    monkeypatch.setattr(ops, "NYXGPT_HOME", tmp_path)  # no k8s/ dir at all

    def _boom(*_a, **_k):
        raise OSError("no config")

    monkeypatch.setattr(ops, "load_config", _boom)
    ops._render_k8s_config_models()  # must not raise


@pytest.mark.parametrize("script", ["k8s-local-smoke.sh", "k8s-artifact-smoke.sh"])
def test_the_smokes_read_the_model_rather_than_restating_it(script: str) -> None:
    """A smoke with its own literal asserts on a model the install may not pull."""
    src = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
    assert "_SHIPPED_CHAT" in src, f"{script} does not read the shipped model"
    literals = re.findall(r"NYXGPT_SMOKE_MODEL:-([^}\s]+)", src)
    assert literals == ["$_SHIPPED_CHAT"], f"{script} restates a model: {literals}"
