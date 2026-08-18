"""Required-model bootstrap: the models every run mode must have before chat works.

nyxGPT cannot answer a single chat message until Ollama holds the configured
chat model, and cannot answer a RAG-enabled one until it also holds the
configured embedding model. RAG is a *per-session* toggle (`rag_enabled`,
overridable from the web UI and the API), so "RAG is off right now" is never a
reason to skip the embedding model: the user can turn it on mid-session and
must not then wait on a download.

This module is the single place that answers "which models does this install
require, and are they here?" -- read from configuration (`[nyxgpt]
default_model`, `[rag] embedding_model`), never hard-coded. Its consumers:

- `nyxgpt.ops.install` (native, `--dev`, `--terraform`) pulls them as an
  install step, so the stack never reports itself up with chat broken (#3824).
- `nyxgpt.ops.status` / `nyxgpt.ops.doctor` report a missing one as a problem.
- The SRE/admin dashboard's model-readiness panel, via
  `/api/v1/models/required`.
- `nyxgpt.rag.embeddings`, whose lazy first-use pull stays as the fallback for
  *per-collection* embedding models the install cannot know about in advance.

The container-run modes carry the same behavior in their own manifests
(`docker-compose.yml`'s ollama entrypoint/healthcheck and
`k8s/statefulset-ollama.yaml`'s postStart/readinessProbe), because there no
`nyxgpt` process runs on the host that could do it for them.
"""

from __future__ import annotations

import logging
import time
from configparser import ConfigParser
from dataclasses import dataclass

from nyxgpt.config import get_default_model, get_ollama_base_url, load_config

logger = logging.getLogger(__name__)

#: Timeout for a single model pull, in seconds.
#:
#: Deliberately a constant and not configuration (#3824): model pulling is
#: internal bootstrap machinery, and the retired `[rag]
#: embedding_pull_timeout_seconds` knob could only be set to break RAG. This is
#: that setting's former default.
MODEL_PULL_TIMEOUT_SECONDS = 600

CHAT_ROLE = "chat"
EMBEDDING_ROLE = "embedding"


@dataclass(frozen=True)
class RequiredModel:
    """A model this install must have in Ollama, and where it was configured."""

    role: str
    """`chat` or `embedding` -- what the model is required *for*."""

    name: str
    """The model as configured, e.g. `qwen3:0.6b`."""

    setting: str
    """The config key it came from, for error messages that name the fix."""


@dataclass(frozen=True)
class ModelPullOutcome:
    """What `ensure_required_models` did about one `RequiredModel`."""

    model: RequiredModel
    ok: bool
    already_present: bool
    detail: str = ""


def normalize_model_name(name: str) -> str:
    """Return `name` with Ollama's implicit `:latest` tag made explicit.

    Ollama's `/api/tags` reports `nomic-embed-text` as `nomic-embed-text:latest`,
    so a configured name without a tag would never match the installed list and
    the bootstrap would re-pull a model that is already there on every run.
    """
    stripped = name.strip()
    if not stripped:
        return ""
    # A digest-pinned or registry-qualified name can contain a colon in the
    # host:port part; only a colon after the last `/` is a tag.
    last_segment = stripped.rsplit("/", 1)[-1]
    if ":" in last_segment:
        return stripped
    return f"{stripped}:latest"


def required_models(cfg: ConfigParser | None = None) -> list[RequiredModel]:
    """Return the models this configuration requires, chat model first.

    The embedding model falls back to the chat model exactly the way
    `nyxgpt.rag.embeddings.get_embedding_config` does, so the two can never
    disagree about what RAG will ask Ollama for. When both resolve to the same
    model it is listed once, under the chat role.
    """
    cfg = cfg if cfg is not None else load_config(None)

    chat = get_default_model(cfg).strip()
    embedding = cfg.get("rag", "embedding_model", fallback="").strip() or chat

    models: list[RequiredModel] = []
    seen: set[str] = set()
    for role, name, setting in (
        (CHAT_ROLE, chat, "[nyxgpt] default_model"),
        (EMBEDDING_ROLE, embedding, "[rag] embedding_model"),
    ):
        if not name:
            continue
        key = normalize_model_name(name)
        if key in seen:
            continue
        seen.add(key)
        models.append(RequiredModel(role=role, name=name, setting=setting))
    return models


def installed_model_names(base_url: str | None = None) -> set[str]:
    """Return the normalized names of every model Ollama currently holds.

    Raises:
        RuntimeError: If Ollama is unreachable (propagated from `list_models`).
    """
    from nyxgpt.models import list_models

    names: set[str] = set()
    for entry in list_models(base_url=base_url):
        name = entry.get("name") or entry.get("model") or ""
        if name:
            names.add(normalize_model_name(str(name)))
    return names


def missing_required_models(
    base_url: str | None = None, cfg: ConfigParser | None = None
) -> list[RequiredModel]:
    """Return the required models Ollama does not have.

    Raises:
        RuntimeError: If Ollama is unreachable -- "cannot tell" is not the same
            answer as "nothing is missing", and callers must not conflate them.
    """
    installed = installed_model_names(base_url=base_url)
    return [m for m in required_models(cfg) if normalize_model_name(m.name) not in installed]


def wait_for_ollama(base_url: str | None = None, timeout_s: float = 120.0) -> bool:
    """Poll Ollama's tag list until it answers, or `timeout_s` elapses.

    Used by the deploy paths that start Ollama in a container and then pull
    into it from the host: `docker run`/`terraform apply` return as soon as the
    container is created, well before `ollama serve` is accepting requests.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            installed_model_names(base_url=base_url)
            return True
        except Exception:  # noqa: BLE001 -- any failure means "not ready yet"
            if time.monotonic() >= deadline:
                return False
            time.sleep(2.0)


def ensure_required_models(
    base_url: str | None = None,
    cfg: ConfigParser | None = None,
    *,
    timeout_s: float = MODEL_PULL_TIMEOUT_SECONDS,
    wait_for_server_s: float = 0.0,
) -> list[ModelPullOutcome]:
    """Pull every required model that Ollama does not already have.

    Idempotent by construction: models already in the store are reported
    `already_present` and nothing is downloaded, so re-running an install over
    a warm machine costs one `/api/tags` request.

    Args:
        base_url: Ollama base URL; read from config when None.
        cfg: Configuration to read the model names from; loaded when None.
        timeout_s: Per-model pull timeout.
        wait_for_server_s: When > 0, wait this long for Ollama to answer before
            giving up -- for callers that just started it.

    Returns:
        One `ModelPullOutcome` per required model, in `required_models` order.
        A failure is reported, never raised: the caller decides whether a
        missing model fails the whole action.
    """
    from nyxgpt.models import pull_model

    cfg = cfg if cfg is not None else load_config(None)
    if base_url is None:
        base_url = get_ollama_base_url(cfg)

    wanted = required_models(cfg)
    if not wanted:
        return []

    if wait_for_server_s > 0 and not wait_for_ollama(base_url, timeout_s=wait_for_server_s):
        return [
            ModelPullOutcome(
                model=m,
                ok=False,
                already_present=False,
                detail=(
                    f"Ollama at {base_url} did not answer within "
                    f"{int(wait_for_server_s)}s -- cannot pull '{m.name}'."
                ),
            )
            for m in wanted
        ]

    try:
        installed = installed_model_names(base_url=base_url)
    except Exception as e:  # noqa: BLE001 -- reported per model, not raised
        return [
            ModelPullOutcome(
                model=m,
                ok=False,
                already_present=False,
                detail=f"Could not list models from Ollama at {base_url}: {e}",
            )
            for m in wanted
        ]

    outcomes: list[ModelPullOutcome] = []
    for model in wanted:
        if normalize_model_name(model.name) in installed:
            outcomes.append(
                ModelPullOutcome(
                    model=model,
                    ok=True,
                    already_present=True,
                    detail=f"'{model.name}' already installed",
                )
            )
            continue
        logger.info(
            "Pulling required %s model '%s' from Ollama at %s",
            model.role,
            model.name,
            base_url,
            extra={"component": "models", "action": "pull", "model": model.name},
        )
        try:
            pull_model(model.name, base_url=base_url, timeout_s=float(timeout_s))
        except Exception as e:  # noqa: BLE001 -- reported per model, not raised
            outcomes.append(
                ModelPullOutcome(
                    model=model,
                    ok=False,
                    already_present=False,
                    detail=(
                        f"Failed to pull the {model.role} model '{model.name}' "
                        f"({model.setting}) from Ollama at {base_url}: {e}"
                    ),
                )
            )
            continue
        outcomes.append(
            ModelPullOutcome(
                model=model,
                ok=True,
                already_present=False,
                detail=f"Pulled '{model.name}'",
            )
        )
    return outcomes


def missing_models_hint(missing: list[RequiredModel]) -> str:
    """Return the operator-facing fix for missing required models.

    Names `nyxgpt` commands only -- never a raw `ollama pull` (Operational
    Command Wrapping).
    """
    names = ", ".join(f"'{m.name}' ({m.role})" for m in missing)
    pulls = " && ".join(f"nyxgpt models pull {m.name}" for m in missing)
    return (
        f"Ollama is missing required model(s): {names}. "
        f"Re-run `nyxgpt ops install` (it pulls them), or pull directly: {pulls}."
    )


__all__ = [
    "CHAT_ROLE",
    "EMBEDDING_ROLE",
    "MODEL_PULL_TIMEOUT_SECONDS",
    "ModelPullOutcome",
    "RequiredModel",
    "ensure_required_models",
    "installed_model_names",
    "missing_models_hint",
    "missing_required_models",
    "normalize_model_name",
    "required_models",
    "wait_for_ollama",
]
