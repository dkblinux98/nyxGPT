"""Unit tests for the Cassandra-backed chat session store (#3590).

Covers:
- `nyxgpt.session_db.CassandraSessionStore` CRUD/list/rename against an
  in-memory fake driver session (no live Cassandra needed),
- the one-time legacy JSON-directory migration (fresh, partial, re-run,
  invalid inputs, marker file),
- the `nyxgpt.sessions` backend dispatch layer with the DB backend active
  (create/read/update/delete/list/pin/tags/title/rename/edit/truncate/
  merge/search/export/batch paths, plus the /sessions endpoints' glue),
- concurrent multi-writer safety (row-level last-write-wins, no torn reads),
- `nyxgpt.config.get_session_backend` resolution.
"""

from __future__ import annotations

import json
import re
import threading
from types import SimpleNamespace

import pytest

from nyxgpt import session_db, sessions
from nyxgpt.config import get_session_backend, load_config
from nyxgpt.session_db import CassandraSessionStore, migrate_sessions_dir

pytestmark = pytest.mark.unit

_COLS = ("name", "messages", "meta", "pinned", "message_count", "updated_at")


class _Rows:
    """Minimal stand-in for a cassandra-driver ResultSet."""

    def __init__(self, rows):
        self._rows = rows

    def one(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class FakeCassandraSession:
    """In-memory emulation of the small CQL surface the store uses.

    Thread-safe (a real driver session is too), so the concurrency test can
    hammer it from multiple writers.
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.queries: list[str] = []

    def execute(self, query, params=None):
        q = " ".join(query.split())
        with self._lock:
            self.queries.append(q)
            if q.startswith("CREATE"):
                return _Rows([])
            if q.startswith("INSERT INTO"):
                m = re.search(r"\(([^)]*)\) VALUES", q)
                assert m, f"unparseable INSERT: {q}"
                names = [c.strip() for c in m.group(1).split(",")]
                assert names[0] == "name"
                row = self.rows.setdefault(params[0], dict.fromkeys(_COLS))
                for col, val in zip(names, params, strict=True):
                    row[col] = val
                return _Rows([])
            if q.startswith("DELETE"):
                self.rows.pop(params[0], None)
                return _Rows([])
            if q.startswith("SELECT"):
                if "WHERE name = %s" in q:
                    row = self.rows.get(params[0])
                    return _Rows([SimpleNamespace(**row)] if row else [])
                return _Rows([SimpleNamespace(**row) for row in self.rows.values()])
            raise AssertionError(f"unexpected query: {q}")


@pytest.fixture
def fake_session():
    return FakeCassandraSession()


@pytest.fixture
def store(fake_session):
    return CassandraSessionStore(session=fake_session, keyspace="testks")


MSGS = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi there"},
]


# ----------------------------
# CassandraSessionStore CRUD
# ----------------------------


class TestStoreCrud:
    def test_save_and_load_messages_roundtrip(self, store):
        store.save_messages("chat1", MSGS)
        assert store.load_messages("chat1") == MSGS

    def test_load_messages_missing_session(self, store):
        assert store.load_messages("nope") == []

    def test_load_messages_filters_invalid_entries(self, store, fake_session):
        raw = [{"role": "user", "content": "ok"}, {"bogus": 1}, "not-a-dict"]
        fake_session.rows["bad"] = dict.fromkeys(_COLS)
        fake_session.rows["bad"].update(name="bad", messages=json.dumps(raw))
        assert store.load_messages("bad") == [{"role": "user", "content": "ok"}]

    def test_load_messages_invalid_json(self, store, fake_session):
        fake_session.rows["bad"] = dict.fromkeys(_COLS)
        fake_session.rows["bad"].update(name="bad", messages="{not json")
        assert store.load_messages("bad") == []

    def test_save_and_load_meta_roundtrip(self, store):
        meta = {"pinned": True, "tags": ["x"], "updated_at": "2026-08-10T00:00:00"}
        store.save_meta("chat1", meta)
        assert store.load_meta("chat1") == meta

    def test_load_meta_missing(self, store):
        assert store.load_meta("nope") == {}

    def test_exists_requires_messages(self, store):
        # Metadata alone does not make a session exist (file-store parity:
        # a session exists when its messages file exists).
        store.save_meta("meta-only", {"pinned": False})
        assert not store.exists("meta-only")
        store.save_messages("meta-only", MSGS)
        assert store.exists("meta-only")

    def test_delete(self, store):
        store.save_messages("chat1", MSGS)
        assert store.delete("chat1") is True
        assert not store.exists("chat1")
        assert store.delete("chat1") is False

    def test_rename(self, store):
        store.save_messages("old", MSGS)
        store.save_meta("old", {"title": "T"})
        ok, msg = store.rename("old", "new")
        assert (ok, msg) == (True, "OK")
        assert not store.exists("old")
        assert store.load_messages("new") == MSGS
        assert store.load_meta("new") == {"title": "T"}

    def test_rename_missing_source(self, store):
        assert store.rename("nope", "new") == (False, "No such session")

    def test_rename_target_exists(self, store):
        store.save_messages("a", MSGS)
        store.save_messages("b", MSGS)
        assert store.rename("a", "b") == (False, "Target session already exists")

    def test_list_sessions_shape_and_pinned_first(self, store):
        store.save_messages("zebra", MSGS)
        store.save_meta("zebra", {"pinned": True, "updated_at": "2026-08-01T10:00:00"})
        store.save_messages("apple", [MSGS[0]])
        store.save_meta("apple", {"pinned": False})
        store.save_meta("meta-only", {"pinned": False})  # never listed

        rows = store.list_sessions()
        assert [r["name"] for r in rows] == ["zebra", "apple"]
        zebra = rows[0]
        assert zebra["messages"] == 2
        assert zebra["meta"]["pinned"] is True
        assert zebra["file"].startswith("cassandra://testks/")
        assert zebra["modified"] == "2026-08-01 10:00:00"

    def test_message_count_tracks_saves(self, store):
        store.save_messages("chat1", MSGS)
        store.save_messages("chat1", MSGS + [{"role": "user", "content": "more"}])
        rows = store.list_sessions()
        assert rows[0]["messages"] == 3


# ----------------------------
# Shut-down session recovery (#3851)
# ----------------------------


class TestShutDownSessionRecovery:
    """The singleton store must not cache a driver session the driver killed.

    `get_session_store()` lives for the life of the API process, so a session
    cached across a Cassandra outage would otherwise be handed back forever
    and no amount of restarting Cassandra would recover the store.
    """

    def _pooled_store(self, monkeypatch, sessions):
        """Return a store whose pool hands out `sessions` in order."""
        from nyxgpt.rag import vectorstore_cassandra as vs

        handed_out = iter(sessions)
        pool = SimpleNamespace(get_session=lambda *a, **k: next(handed_out))
        monkeypatch.setattr(vs, "get_connection_pool", lambda cfg: pool)
        return CassandraSessionStore(keyspace="testks")

    def test_shut_down_session_is_replaced(self, monkeypatch):
        dead, live = FakeCassandraSession(), FakeCassandraSession()
        dead.is_shutdown = False
        live.is_shutdown = False
        store = self._pooled_store(monkeypatch, [dead, live])

        store.save_messages("chat1", MSGS)
        assert dead.rows  # first write landed on the first session

        dead.is_shutdown = True
        store.save_messages("chat2", MSGS)

        assert "chat2" in live.rows, "the write must go to the replacement session"
        assert "chat2" not in dead.rows
        assert store._session is live

    def test_live_session_is_reused(self, monkeypatch):
        live = FakeCassandraSession()
        live.is_shutdown = False
        store = self._pooled_store(monkeypatch, [live])

        store.save_messages("chat1", MSGS)
        store.save_messages("chat2", MSGS)

        assert store._session is live  # no spurious reconnect

    def test_injected_session_without_shutdown_flag_is_reused(self, store, fake_session):
        """A stand-in session with no `is_shutdown` attribute counts as usable."""
        store.save_messages("chat1", MSGS)
        store.save_messages("chat2", MSGS)
        assert store._session is fake_session


# ----------------------------
# Legacy directory migration
# ----------------------------


def _write_legacy(tmp_path, name, messages, meta=None):
    (tmp_path / f"{name}.json").write_text(json.dumps(messages), encoding="utf-8")
    if meta is not None:
        (tmp_path / f"{name}.meta.json").write_text(json.dumps(meta), encoding="utf-8")


class TestMigration:
    def test_fresh_import(self, store, tmp_path):
        _write_legacy(tmp_path, "alpha", MSGS, {"title": "A"})
        _write_legacy(tmp_path, "beta", [MSGS[0]])

        report = migrate_sessions_dir(tmp_path, store=store)

        assert sorted(report["migrated"]) == ["alpha", "beta"]
        assert report["skipped_existing"] == []
        assert store.load_messages("alpha") == MSGS
        assert store.load_meta("alpha") == {"title": "A"}
        assert store.exists("beta")
        # Source files are kept as a read-only archive
        assert (tmp_path / "alpha.json").exists()
        # Marker file records the report
        marker = json.loads((tmp_path / session_db.MIGRATION_MARKER).read_text())
        assert sorted(marker["report"]["migrated"]) == ["alpha", "beta"]

    def test_partial_import_skips_existing(self, store, tmp_path):
        store.save_messages("alpha", [{"role": "user", "content": "db copy"}])
        _write_legacy(tmp_path, "alpha", MSGS)
        _write_legacy(tmp_path, "beta", MSGS)

        report = migrate_sessions_dir(tmp_path, store=store)

        assert report["migrated"] == ["beta"]
        assert report["skipped_existing"] == ["alpha"]
        # Existing DB content is never overwritten
        assert store.load_messages("alpha") == [{"role": "user", "content": "db copy"}]

    def test_rerun_is_idempotent(self, store, tmp_path):
        _write_legacy(tmp_path, "alpha", MSGS)
        first = migrate_sessions_dir(tmp_path, store=store)
        second = migrate_sessions_dir(tmp_path, store=store)

        assert first["migrated"] == ["alpha"]
        assert second["migrated"] == []
        assert second["skipped_existing"] == ["alpha"]
        assert store.load_messages("alpha") == MSGS

    def test_invalid_name_and_invalid_json_skipped(self, store, tmp_path):
        (tmp_path / "bad name!.json").write_text("[]", encoding="utf-8")
        (tmp_path / "notalist.json").write_text('{"role": "user"}', encoding="utf-8")
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

        report = migrate_sessions_dir(tmp_path, store=store)

        assert report["migrated"] == []
        assert sorted(report["skipped_invalid"]) == ["bad name!", "broken", "notalist"]

    def test_missing_directory_is_noop(self, store, tmp_path):
        report = migrate_sessions_dir(tmp_path / "does-not-exist", store=store)
        assert report["migrated"] == []


# ----------------------------
# sessions.py dispatch layer (DB backend active)
# ----------------------------


@pytest.fixture
def db_backend(monkeypatch, fake_session, store):
    """Activate the Cassandra backend with an in-memory fake store."""
    monkeypatch.setenv("NYXGPT_SESSION_BACKEND", "cassandra")
    monkeypatch.setattr(session_db, "_store_instance", store)
    yield store


class TestDispatch:
    def test_primitives_never_touch_disk(self, db_backend, tmp_path):
        sf = sessions.session_file_for("chat1", tmp_path)
        sessions.save_session_messages(sf, MSGS)
        assert sessions.load_session_messages(sf) == MSGS
        assert not sf.exists()  # nothing written to disk

        mf = sessions.meta_file_for(sf)
        sessions.save_session_meta(mf, {"pinned": True})
        assert sessions.load_session_meta(mf) == {"pinned": True}
        assert not mf.exists()

    def test_paginated_load(self, db_backend, tmp_path):
        sf = sessions.session_file_for("chat1", tmp_path)
        sessions.save_session_messages(sf, MSGS)
        page, total = sessions.load_session_messages_paginated(sf, offset=1, limit=5)
        assert (page, total) == ([MSGS[1]], 2)

    def test_session_exists_and_lock(self, db_backend, tmp_path):
        assert not sessions.session_exists("chat1", tmp_path)
        sf = sessions.session_file_for("chat1", tmp_path)
        sessions.save_session_messages(sf, MSGS)
        assert sessions.session_exists("chat1", tmp_path)
        # Lock is a no-op context manager under the DB backend
        with sessions.session_lock(sf, timeout=0.1):
            pass
        assert not sf.exists()

    def test_init_persist_list_delete_cycle(self, db_backend, tmp_path):
        state = sessions.load_session(
            "cycle", load_config(None), sessions_dir_override=str(tmp_path), new_session=True
        )
        state.messages.extend(MSGS)
        sessions.save_session(state, load_config(None))

        rows = sessions.list_sessions(None)
        assert [r["name"] for r in rows] == ["cycle"]
        assert rows[0]["messages"] == 2

        assert sessions.delete_session("cycle", tmp_path) is True
        assert sessions.list_sessions(None) == []

    def test_new_session_resets_existing(self, db_backend, tmp_path):
        sf = sessions.session_file_for("chat1", tmp_path)
        sessions.save_session_messages(sf, MSGS)
        _sf, _mf, msgs, _meta = sessions.init_session(
            "chat1", tmp_path, new_session=True, model="m"
        )
        assert msgs == []
        assert db_backend.load_messages("chat1") == []

    def test_pin_title_tags(self, db_backend, tmp_path):
        sf = sessions.session_file_for("chat1", tmp_path)
        sessions.save_session_messages(sf, MSGS)

        ok, _ = sessions.set_pinned("chat1", True, tmp_path)
        assert ok
        assert db_backend.load_meta("chat1")["pinned"] is True

        ok, _ = sessions.set_title("chat1", "My Title", tmp_path)
        assert ok
        ok, _ = sessions.add_tags("chat1", ["b", "a"], tmp_path)
        assert ok
        ok, _ = sessions.remove_tags("chat1", ["b"], tmp_path)
        assert ok
        meta = db_backend.load_meta("chat1")
        assert meta["title"] == "My Title"
        assert meta["tags"] == ["a"]

    def test_pin_missing_session(self, db_backend, tmp_path):
        ok, msg = sessions.set_pinned("ghost", True, tmp_path)
        assert (ok, msg) == (False, "No such session")

    def test_rename_session(self, db_backend, tmp_path):
        sf = sessions.session_file_for("old", tmp_path)
        sessions.save_session_messages(sf, MSGS)
        assert sessions.rename_session("old", "new", tmp_path) == (True, "OK")
        assert not sessions.session_exists("old", tmp_path)
        assert sessions.session_exists("new", tmp_path)

    def test_edit_and_truncate(self, db_backend, tmp_path):
        sf = sessions.session_file_for("chat1", tmp_path)
        sessions.save_session_messages(sf, MSGS + [{"role": "user", "content": "third"}])
        ok, _ = sessions.edit_message("chat1", 1, "edited", tmp_path, fork=False)
        assert ok
        msgs = db_backend.load_messages("chat1")
        assert msgs[1]["content"] == "edited"
        assert msgs[1]["original_content"] == "hi there"

        ok, _ = sessions.truncate_after_message("chat1", 0, tmp_path)
        assert ok
        assert len(db_backend.load_messages("chat1")) == 1

    def test_merge_sessions(self, db_backend, tmp_path):
        sessions.save_session_messages(sessions.session_file_for("a", tmp_path), [MSGS[0]])
        sessions.save_session_messages(sessions.session_file_for("b", tmp_path), [MSGS[1]])
        ok, msg = sessions.merge_sessions(["a", "b"], "merged", tmp_path)
        assert ok, msg
        assert len(db_backend.load_messages("merged")) == 2

    def test_search_messages(self, db_backend, tmp_path):
        sf = sessions.session_file_for("chat1", tmp_path)
        sessions.save_session_messages(sf, MSGS)
        sessions.save_session_meta(
            sessions.meta_file_for(sf), {"title": "Greetings", "pinned": False}
        )
        results = sessions.search_messages("hello", tmp_path)
        assert len(results) == 1
        assert results[0]["session_name"] == "chat1"
        assert results[0]["session_title"] == "Greetings"

        assert sessions.search_messages("hello", tmp_path, role_filter="assistant") == []
        assert sessions.search_messages("hi", tmp_path, session_filter="other-session") == []

    def test_export_json(self, db_backend, tmp_path):
        sf = sessions.session_file_for("chat1", tmp_path)
        sessions.save_session_messages(sf, MSGS)
        ok, payload = sessions.export_session_json("chat1", tmp_path)
        assert ok
        data = json.loads(payload)
        assert data["name"] == "chat1"
        assert data["messages"] == MSGS

    def test_export_missing_session(self, db_backend, tmp_path):
        ok, msg = sessions.export_session_markdown("ghost", tmp_path)
        assert (ok, msg) == (False, "No such session")

    def test_sync_filename_with_title(self, db_backend, tmp_path):
        sf = sessions.session_file_for("session-123", tmp_path)
        sessions.save_session_messages(sf, MSGS)
        sessions.save_session_meta(
            sessions.meta_file_for(sf), {"title": "My Chat Session", "pinned": False}
        )
        ok, status, new_name = sessions.sync_filename_with_title(
            "session-123", tmp_path, force=True
        )
        assert (ok, status, new_name) == (True, "renamed", "my-chat-session")
        assert db_backend.exists("my-chat-session")
        assert not db_backend.exists("session-123")

    def test_batch_operations(self, db_backend, tmp_path):
        for name in ("a", "b"):
            sessions.save_session_messages(sessions.session_file_for(name, tmp_path), MSGS)
        okc, failc, failed = sessions.batch_update_metadata(
            ["a", "b", "ghost"], tmp_path, pinned=True
        )
        assert (okc, failc, failed) == (2, 1, ["ghost"])
        okc, failc, failed = sessions.batch_delete_sessions(["a", "ghost"], tmp_path)
        assert (okc, failc, failed) == (1, 1, ["ghost"])

    def test_sessions_endpoint_lists_db_sessions(self, db_backend, tmp_path):
        from fastapi.testclient import TestClient

        from nyxgpt.app import app

        sf = sessions.session_file_for("api-session", tmp_path)
        sessions.save_session_messages(sf, MSGS)

        client = TestClient(app)
        resp = client.get("/api/v1/sessions")
        assert resp.status_code == 200
        names = [s["name"] for s in resp.json()["sessions"]]
        assert names == ["api-session"]


# ----------------------------
# Concurrent multi-writer safety
# ----------------------------


class TestConcurrency:
    def test_concurrent_writers_last_write_wins_no_torn_reads(self, db_backend, tmp_path):
        """Simulates multiple API instances writing the same session.

        Every read must observe exactly one writer's complete document --
        never an interleaving -- and no writer may raise.
        """
        sf = sessions.session_file_for("shared", tmp_path)
        errors: list[Exception] = []
        docs = {
            i: [{"role": "user", "content": f"writer-{i}-msg-{j}"} for j in range(5)]
            for i in range(8)
        }

        def writer(i: int) -> None:
            try:
                for _ in range(20):
                    sessions.save_session_messages(sf, docs[i])
            except Exception as e:  # pragma: no cover - failure path
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in docs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        final = sessions.load_session_messages(sf)
        assert final in docs.values()  # one complete document, no tearing

    def test_concurrent_distinct_sessions(self, db_backend, tmp_path):
        def writer(i: int) -> None:
            sf = sessions.session_file_for(f"s{i}", tmp_path)
            sessions.save_session_messages(sf, [{"role": "user", "content": str(i)}])

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(sessions.list_sessions(None)) == 10


# ----------------------------
# Config resolution
# ----------------------------


class TestBackendConfig:
    def test_default_is_file(self, monkeypatch):
        monkeypatch.delenv("NYXGPT_SESSION_BACKEND", raising=False)
        cfg = load_config(None)
        if cfg.has_option("nyxgpt", "session_backend"):
            cfg.remove_option("nyxgpt", "session_backend")
        assert get_session_backend(cfg) == "file"

    def test_config_value(self, monkeypatch):
        monkeypatch.delenv("NYXGPT_SESSION_BACKEND", raising=False)
        cfg = load_config(None)
        cfg.set("nyxgpt", "session_backend", "cassandra")
        try:
            assert get_session_backend(cfg) == "cassandra"
        finally:
            cfg.remove_option("nyxgpt", "session_backend")

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("NYXGPT_SESSION_BACKEND", "cassandra")
        cfg = load_config(None)
        assert get_session_backend(cfg) == "cassandra"

    def test_invalid_value_falls_back_to_file(self, monkeypatch):
        monkeypatch.setenv("NYXGPT_SESSION_BACKEND", "postgres")
        cfg = load_config(None)
        assert get_session_backend(cfg) == "file"
