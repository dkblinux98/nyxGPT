

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable

from mygpt.config import get_default_model, get_ollama_base_url, load_config


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    model: str
    dimension: int


class EmbeddingError(RuntimeError):
    pass


def _embedding_cfg() -> EmbeddingConfig:
    cfg = load_config(None)
    base_url = get_ollama_base_url(cfg).rstrip("/")

    # Allow dedicated embedding model override; otherwise fall back to default_model.
    # [rag] embedding_model = ...
    model = cfg.get("rag", "embedding_model", fallback="").strip() or get_default_model(cfg)

    # Default dimension must match Cassandra schema; override in config if needed.
    dim = cfg.getint("rag", "embedding_dim", fallback=768)

    return EmbeddingConfig(base_url=base_url, model=model, dimension=int(dim))


def _post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise EmbeddingError(f"HTTP error calling {url}: {e.code} {msg}")
    except urllib.error.URLError as e:
        raise EmbeddingError(f"Failed to reach Ollama at {url}: {e}")


def embed_texts(texts: Iterable[str]) -> list[list[float]]:
    """Embed a batch of texts using Ollama.

    Uses the `/api/embed` endpoint.

    Config:
      - `[ollama] base_url`
      - `[rag] embedding_model` (optional)
      - `[rag] embedding_dim` (must match Cassandra table schema)

    Returns: list of float vectors, one per input text.
    """

    texts_list = [t if isinstance(t, str) else str(t) for t in texts]
    if not texts_list:
        return []

    ecfg = _embedding_cfg()
    url = f"{ecfg.base_url}/api/embed"

    data = _post_json(url, {"model": ecfg.model, "input": texts_list})

    # Ollama returns either "embeddings" (batch) or "embedding" (single)
    if "embeddings" in data:
        vectors = data["embeddings"]
    elif "embedding" in data:
        vectors = [data["embedding"]]
    else:
        raise EmbeddingError(f"Unexpected Ollama embed response keys: {list(data.keys())}")

    # Validate dimensions
    out: list[list[float]] = []
    for i, v in enumerate(vectors):
        if not isinstance(v, list):
            raise EmbeddingError(f"Embedding {i} is not a list")
        if len(v) != ecfg.dimension:
            raise EmbeddingError(
                f"Embedding {i} has dim {len(v)} but expected {ecfg.dimension}. "
                f"Update Cassandra schema and/or [rag] embedding_dim to match."
            )
        out.append([float(x) for x in v])

    if len(out) != len(texts_list):
        raise EmbeddingError(
            f"Embedding count mismatch: got {len(out)} vectors for {len(texts_list)} inputs"
        )

    return out


def embed_text(text: str) -> list[float]:
    """Convenience wrapper for a single string."""
    vecs = embed_texts([text])
    return vecs[0] if vecs else []