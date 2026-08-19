"""Fault-injected tests: the RAG collection endpoints on a keyspace-less Cassandra (#3864).

Owner acceptance of the rc12 cloud path found that a freshly provisioned
instance cannot use the RAG Collections page at all: `GET
/api/v1/rag/collections` returned HTTP 500 and Create Collection failed with
``Keyspace 'nyxgpt' does not exist``, because only the ingest path ever
created the keyspace. Until someone ingested a document, both endpoints were
dead.

The fault injected here is exactly that condition: a Cassandra stand-in that
holds *no* keyspaces and raises the driver's real ``InvalidRequest`` on ``USE
<missing keyspace>``, the way a real empty cluster does. The rest of the
sibling suites mock `CassandraVectorStore` itself, which cannot see this bug
-- the failure was inside the store, in `list_collections`'s `USE`. So these
tests drive the *real* store class and mock only the driver beneath it.

Pre-fix, `test_list_collections_on_fresh_cassandra_*` and
`test_create_collection_on_fresh_cassandra_*` both fail (500 / "does not
exist"); post-fix both pass. Executed evidence against a real empty Cassandra
lives in `.github/workflows/rag-fresh-cassandra-smoke.yml`.
"""

from __future__ import annotations

from collections.abc import Iterator
from configparser import ConfigParser
from typing import Any

import pytest
from cassandra import InvalidRequest
from fastapi.testclient import TestClient

from nyxgpt.app import app

pytestmark = pytest.mark.unit

KEYSPACE = "nyxgpt"
BASE_TABLE = "rag_chunks"


class _Row:
    """Row stand-in exposing driver-style attribute access."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class _Rows(list):
    """ResultSet stand-in: iterable, with the driver's `.one()`."""

    def one(self) -> Any | None:
        return self[0] if self else None


class FreshCassandraSession:
    """In-memory Cassandra whose keyspaces start out empty.

    Models only what the collection endpoints touch, but models the one thing
    that matters faithfully: ``USE <keyspace>`` raises ``InvalidRequest`` when
    the keyspace has not been created, exactly as a real cluster does. Every
    statement executed is recorded in `self.executed` so a test can assert
    that a code path never selected a keyspace at all.
    """

    is_shutdown = False

    def __init__(self) -> None:
        self.keyspaces: set[str] = set()
        self.tables: dict[str, set[str]] = {}
        self.current_keyspace: str | None = None
        self.executed: list[str] = []
        self.settings: dict[str, tuple] = {}

    # -- helpers -----------------------------------------------------------

    def _qualify(self, name: str) -> tuple[str | None, str]:
        """Split ``ks.table`` / bare ``table`` into (keyspace, table)."""
        name = name.strip().strip(";").strip()
        if "." in name:
            ks, _, tbl = name.partition(".")
            return ks, tbl
        return self.current_keyspace, name

    def _table_exists(self, name: str) -> bool:
        ks, tbl = self._qualify(name)
        return ks is not None and tbl in self.tables.get(ks, set())

    # -- driver surface ----------------------------------------------------

    def prepare(self, query: str) -> Any:  # pragma: no cover - unused here
        raise AssertionError("collection endpoints should not prepare statements")

    def execute(self, query: Any, params: Any = None, **_kwargs: Any) -> _Rows:
        text = str(getattr(query, "query_string", query)).strip()
        self.executed.append(text)
        words = text.replace("(", " ( ").split()
        upper = [w.upper() for w in words]
        joined = " ".join(upper)

        if upper[:1] == ["USE"]:
            ks = words[1].strip(";")
            if ks not in self.keyspaces:
                raise InvalidRequest(f"Keyspace '{ks}' does not exist")
            self.current_keyspace = ks
            return _Rows()

        if upper[:2] == ["CREATE", "KEYSPACE"]:
            # `CREATE KEYSPACE [IF NOT EXISTS] <ks> WITH REPLICATION = {...}`
            idx = upper.index("EXISTS") + 1 if "EXISTS" in upper else 2
            self.keyspaces.add(words[idx].strip(";"))
            return _Rows()

        if upper[:2] == ["CREATE", "TABLE"]:
            idx = upper.index("EXISTS") + 1 if "EXISTS" in upper else 2
            ks, tbl = self._qualify(words[idx])
            if ks is None:
                raise InvalidRequest("No keyspace has been specified")
            if ks not in self.keyspaces:
                raise InvalidRequest(f"Keyspace '{ks}' does not exist")
            self.tables.setdefault(ks, set()).add(tbl)
            return _Rows()

        if upper[:2] == ["CREATE", "INDEX"] or upper[:2] == ["ALTER", "TABLE"]:
            return _Rows()

        if "SYSTEM_SCHEMA.TABLES" in joined:
            ks = (params or [None])[0]
            return _Rows(_Row(table_name=t) for t in sorted(self.tables.get(ks, set())))

        if "SYSTEM_SCHEMA.COLUMNS" in joined:
            return _Rows()

        if "SYSTEM.LOCAL" in joined:  # pool health check
            return _Rows([_Row(now=1)])

        if "COLLECTION_SETTINGS" in joined:
            return self._collection_settings(upper, params)

        # Any other read is against a collection table.
        target = words[upper.index("FROM") + 1] if "FROM" in upper else ""
        if not self._table_exists(target):
            raise InvalidRequest(f"unconfigured table {target}")
        return _Rows()

    def _collection_settings(self, upper: list[str], params: Any) -> _Rows:
        if upper[:1] == ["INSERT"]:
            values = list(params or [])
            self.settings[values[0]] = tuple(values[1:])
            return _Rows()
        if upper[:1] == ["DELETE"]:
            self.settings.pop((params or [None])[0], None)
            return _Rows()
        saved = self.settings.get((params or [None])[0])
        if saved is None:
            return _Rows()
        return _Rows([_Row(embedding_model=saved[0], chunk_size=saved[1], chunk_overlap=saved[2])])


@pytest.fixture
def fresh_cassandra(monkeypatch: pytest.MonkeyPatch) -> Iterator[FreshCassandraSession]:
    """Point the real CassandraVectorStore at an empty in-memory cluster."""
    from nyxgpt.rag import vectorstore_cassandra as vs

    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_keyspace": KEYSPACE, "cassandra_table": BASE_TABLE}
    monkeypatch.setattr(vs, "load_config", lambda *_a, **_k: cfg)

    session = FreshCassandraSession()

    class _Cluster:
        def __init__(self, _hosts: Any, **_kwargs: Any) -> None:
            pass

        def connect(self, *_a: Any, **_k: Any) -> FreshCassandraSession:
            return session

        def shutdown(self) -> None:
            pass

    monkeypatch.setattr(vs, "Cluster", _Cluster)

    # The pool is a module-level singleton keyed on config; drop it so this
    # test's session is the one handed out, and again afterwards so the fake
    # does not leak into another test.
    vs.reset_connection_pool()
    yield session
    vs.reset_connection_pool()


def _client() -> TestClient:
    return TestClient(app)


# ----------------------------
# GET /rag/collections
# ----------------------------


def test_list_collections_on_fresh_cassandra_returns_empty_200(
    fresh_cassandra: FreshCassandraSession,
) -> None:
    """A cluster with no keyspace has no collections -- 200 and [], not 500."""
    resp = _client().get("/api/v1/rag/collections")

    assert resp.status_code == 200, resp.text
    assert resp.json()["collections"] == []


def test_list_collections_never_selects_the_keyspace(
    fresh_cassandra: FreshCassandraSession,
) -> None:
    """Regression pin: listing must not issue `USE`, the statement that 500'd."""
    _client().get("/api/v1/rag/collections")

    assert not any(stmt.upper().startswith("USE ") for stmt in fresh_cassandra.executed)


# ----------------------------
# POST /rag/collections
# ----------------------------


def test_create_collection_on_fresh_cassandra_creates_keyspace_and_table(
    fresh_cassandra: FreshCassandraSession,
) -> None:
    """Create works before anything has ever been ingested."""
    resp = _client().post(
        "/api/v1/rag/collections",
        json={"name": "first_coll", "embedding_dim": 768},
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["collection"] == "first_coll"
    assert KEYSPACE in fresh_cassandra.keyspaces
    assert f"{BASE_TABLE}_first_coll" in fresh_cassandra.tables[KEYSPACE]


def test_collection_created_on_fresh_cassandra_then_lists(
    fresh_cassandra: FreshCassandraSession,
) -> None:
    """End-to-end on a clean deployment: create, then see it on the page."""
    client = _client()
    created = client.post(
        "/api/v1/rag/collections",
        json={"name": "second_coll", "embedding_dim": 384, "embedding_model": "nomic-embed-text"},
    )
    assert created.status_code == 201, created.text

    listed = client.get("/api/v1/rag/collections")

    assert listed.status_code == 200, listed.text
    collections = listed.json()["collections"]
    assert [c["name"] for c in collections] == ["second_coll"]
    assert collections[0]["embedding_model"] == "nomic-embed-text"


def test_duplicate_check_still_returns_409_after_creation(
    fresh_cassandra: FreshCassandraSession,
) -> None:
    """Tolerating a missing keyspace must not cost the duplicate check.

    The check necessarily runs before `ensure_schema` (which is
    CREATE ... IF NOT EXISTS and so has nothing left to conflict on), so this
    pins that ordering as well as the 409.
    """
    client = _client()
    body = {"name": "dupe_coll", "embedding_dim": 768}
    assert client.post("/api/v1/rag/collections", json=body).status_code == 201

    second = client.post("/api/v1/rag/collections", json=body)

    assert second.status_code == 409
    assert "already exists" in second.text
