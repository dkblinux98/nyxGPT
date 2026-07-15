"""Cross-module test for the fresh Docker Compose install chat path (#3182).

On a fresh install, `enable_chat_context = true` is the Compose default but
no Cassandra keyspace exists yet (nothing has been RAG-ingested). These
tests exercise the real chat.py -> rag.py -> vectorstore_cassandra.py chain
against a mocked Cassandra driver that raises the same
`InvalidRequest("... does not exist")` the real driver raises for a missing
keyspace, verifying chat degrades to empty RAG context instead of raising
(which previously surfaced as a 500 on every chat request), and that the
first RAG ingest bootstraps the schema instead of hitting the same error.
"""

from __future__ import annotations

from configparser import ConfigParser
from unittest.mock import Mock

import pytest


def _fresh_install_cfg() -> ConfigParser:
    cfg = ConfigParser()
    cfg["nyxgpt"] = {"default_model": "qwen2.5:0.5b"}
    cfg["rag"] = {
        "enable_chat_context": "true",
        "cassandra_keyspace": "nyxgpt",
        "cassandra_table": "rag_chunks",
        "embedding_model": "nomic-embed-text",
        "embedding_dim": "3",
    }
    return cfg


def _patch_config(monkeypatch: pytest.MonkeyPatch, cfg: ConfigParser) -> None:
    for target in (
        "nyxgpt.chat.load_config",
        "nyxgpt.sessions.load_config",
        "nyxgpt.rag.rag.load_config",
        "nyxgpt.rag.embeddings.load_config",
        "nyxgpt.rag.vectorstore_cassandra.load_config",
    ):
        monkeypatch.setattr(target, lambda *_a, **_k: cfg)


def _patch_missing_keyspace_cassandra(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Patch the Cassandra driver so session.execute()/prepare() raise the
    same InvalidRequest a real cluster raises for a keyspace that hasn't
    been created yet."""
    from cassandra import InvalidRequest

    mock_session = Mock()
    mock_session.execute.side_effect = InvalidRequest("Keyspace 'nyxgpt' does not exist")
    mock_session.prepare.side_effect = InvalidRequest("Keyspace 'nyxgpt' does not exist")

    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda hosts, **kwargs: mock_cluster
    )
    return mock_session


def _patch_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid real HTTP calls to Ollama's /api/embed."""
    monkeypatch.setattr(
        "nyxgpt.rag.embeddings._post_json",
        lambda url, payload, timeout: {"embedding": [0.1, 0.2, 0.3]},
    )


@pytest.mark.unit
def test_chat_context_on_fresh_install_degrades_to_empty_rag(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """_prepare_chat_context must not raise when RAG is enabled but the
    Cassandra keyspace doesn't exist yet -- it should build a normal chat
    context with zero RAG chunks."""
    cfg = _fresh_install_cfg()
    _patch_config(monkeypatch, cfg)
    _patch_embeddings(monkeypatch)
    _patch_missing_keyspace_cassandra(monkeypatch)

    from nyxgpt.chat import _prepare_chat_context

    context = _prepare_chat_context(
        "Hello, is anyone there?",
        session="fresh-install-test",
        sessions_dir=str(tmp_path),
    )

    assert context.rag_used is True  # enable_chat_context = true
    assert context.rag_chunks == 0
    assert context.rag_context == []
    # No RAG system message should have been injected since there was no context.
    assert len(context.messages) == 1
    assert context.messages[0]["role"] == "user"


@pytest.mark.unit
def test_chat_on_fresh_install_returns_reply_instead_of_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The full chat() call -- context prep, LLM call, persistence -- must
    complete and return a reply on a fresh install instead of raising."""
    cfg = _fresh_install_cfg()
    _patch_config(monkeypatch, cfg)
    _patch_embeddings(monkeypatch)
    _patch_missing_keyspace_cassandra(monkeypatch)

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", lambda **_kwargs: "Hello! How can I help?")

    from nyxgpt.chat import chat

    result = chat(
        "Hello, is anyone there?",
        session="fresh-install-test",
        sessions_dir=str(tmp_path),
    )

    assert result.reply == "Hello! How can I help?"
    assert result.rag_used is True
    assert result.rag_chunks == 0


@pytest.mark.unit
def test_first_rag_ingest_on_fresh_install_bootstraps_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first RAG ingest on a fresh install (no ensure_schema flag passed
    by the caller, matching the API/CLI default) must create the schema
    automatically rather than failing with "keyspace does not exist"."""
    cfg = _fresh_install_cfg()
    _patch_config(monkeypatch, cfg)
    _patch_embeddings(monkeypatch)

    from cassandra import InvalidRequest

    # A minimal stateful fake: every query against the target
    # keyspace/table raises "does not exist" until the CREATE
    # KEYSPACE/TABLE statements (issued by ensure_schema()) run, exactly
    # like a real fresh cluster.
    schema_state = {"created": False}

    def fake_execute(statement, *_args, **_kwargs):
        # `statement` is a plain f-string for the CREATE calls issued by
        # ensure_schema(), a real SimpleStatement (with a real
        # .query_string) for the system_schema/doc_hash lookups, or a
        # mocked PreparedStatement for the final chunk insert -- only the
        # first two carry real query text, so anything else just falls
        # through to the generic success branch below.
        if isinstance(statement, str):
            text = statement
        else:
            query_string = getattr(statement, "query_string", None)
            text = query_string if isinstance(query_string, str) else ""
        if "system_schema.tables" in text:
            result = Mock()
            result.one.return_value = Mock() if schema_state["created"] else None
            return result
        if "CREATE KEYSPACE" in text or "CREATE TABLE" in text:
            schema_state["created"] = True
            return Mock()
        if "CREATE INDEX" in text:
            return Mock()
        if not schema_state["created"]:
            raise InvalidRequest("Keyspace 'nyxgpt' does not exist")
        if "SELECT doc_hash" in text:
            result = Mock()
            result.one.return_value = None  # document doesn't exist yet
            return result
        return Mock()

    mock_session = Mock()
    mock_session.execute.side_effect = fake_execute

    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda hosts, **kwargs: mock_cluster
    )

    from nyxgpt.rag.rag import ingest_document

    result = ingest_document("first-doc", "Some fresh-install content to ingest.")

    assert result["status"] == "ingested"
    assert result["chunks_ingested"] >= 1
    assert schema_state["created"] is True
