"""Cassandra-backed chat session store (#3590).

Chat sessions were historically stored as one JSON file per session under
``[nyxgpt] sessions_dir`` (default ``~/.nyxGPT/sessions``). That forks session
state per deployment mode: the native install, the Compose stack, the
Terraform-managed containers, and Kubernetes each see their own disk, so the
sessions sidebar shows a different history in every mode. This module stores
sessions in the Cassandra instance that is already a required core service of
the stack (the same one the RAG vector store uses), so every deployment mode
pointed at the same Cassandra sees the same sessions.

Schema (in the ``[rag] cassandra_keyspace`` keyspace, default ``nyxgpt``):

    CREATE TABLE chat_sessions (
        name          text PRIMARY KEY,
        messages      text,     -- JSON array, same shape as the session file
        meta          text,     -- JSON object, same shape as the .meta.json file
        pinned        boolean,  -- denormalized from meta for cheap list sorting
        message_count int,      -- denormalized so listing never loads messages
        updated_at    text      -- ISO 8601, denormalized from meta
    )

One row per session mirrors the file store's whole-document read/write
semantics exactly, which keeps the higher-level operations in
``nyxgpt.sessions`` byte-compatible across backends. Cassandra guarantees
row-level (single partition) write atomicity, so concurrent writers from
multiple API instances can never interleave a torn session document --
last write wins, which is the same policy the file store's atomic
rename-into-place gives a single host, now safe across hosts.

A session "exists" when its row has a non-null ``messages`` column -- the
exact analogue of the file store, where a session exists when its messages
file exists (metadata may be written first and alone).

Backend selection lives in ``nyxgpt.config.get_session_backend`` and the
dispatch layer in ``nyxgpt.sessions``; this module deliberately never imports
``nyxgpt.sessions`` (the migration reads legacy JSON files directly) so there
is no import cycle.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Table name inside the existing RAG keyspace. Cassandra caps unquoted
# identifiers at 48 chars; this is well under.
SESSIONS_TABLE = "chat_sessions"

# Marker file written into a legacy sessions directory after a migration
# pass. The directory itself is kept as a read-only archive (documented
# decision -- see docs/session-storage.md).
MIGRATION_MARKER = ".migrated-to-db.json"

_VALID_SESSION_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class SessionStoreError(RuntimeError):
    """Raised when the Cassandra session store cannot serve a request."""


def _iso_now() -> str:
    """Return the current local time as an ISO 8601 string (second precision)."""
    return datetime.now().isoformat(timespec="seconds")


class CassandraSessionStore:
    """CRUD/list/rename operations for chat sessions in Cassandra.

    Connects lazily through the shared RAG connection pool
    (:func:`nyxgpt.rag.vectorstore_cassandra.get_connection_pool`) on first
    use; tests inject a fake driver session via the constructor instead.
    """

    def __init__(self, session: Any | None = None, keyspace: str | None = None) -> None:
        """Create a store, optionally with an injected driver session (tests).

        Args:
            session: A Cassandra driver ``Session``-like object exposing
                ``execute(query, params)``. When None, a real connection is
                established lazily via the shared RAG connection pool.
            keyspace: Keyspace override; defaults to ``[rag] cassandra_keyspace``.
        """
        self._injected_session = session
        self._keyspace_override = keyspace
        self._lock = threading.Lock()
        self._session: Any | None = None
        self._keyspace: str | None = None
        self._schema_ready = False

    # -- connection / schema -------------------------------------------------

    def _connect(self) -> tuple[Any, str]:
        """Return (driver session, keyspace), connecting via the RAG pool."""
        if self._injected_session is not None:
            return self._injected_session, self._keyspace_override or "nyxgpt"

        from nyxgpt.rag.vectorstore_cassandra import _cassandra_cfg, get_connection_pool

        cfg = _cassandra_cfg()
        try:
            driver_session = get_connection_pool(cfg).get_session()
        except Exception as exc:
            raise SessionStoreError(
                f"Cannot reach Cassandra for the session store " f"({cfg.hosts}:{cfg.port}): {exc}"
            ) from exc
        return driver_session, self._keyspace_override or cfg.keyspace

    def _ensure_schema(self, driver_session: Any, keyspace: str) -> None:
        """Create the keyspace and sessions table if they don't exist."""
        driver_session.execute(
            f"CREATE KEYSPACE IF NOT EXISTS {keyspace} "
            f"WITH REPLICATION = {{'class':'SimpleStrategy','replication_factor':1}};"
        )
        driver_session.execute(
            f"CREATE TABLE IF NOT EXISTS {keyspace}.{SESSIONS_TABLE} ("
            f"name text PRIMARY KEY, messages text, meta text, "
            f"pinned boolean, message_count int, updated_at text);"
        )

    def _conn(self) -> tuple[Any, str]:
        """Return a ready (session, keyspace) pair, initializing schema once.

        This store is a process-lifetime singleton (:func:`get_session_store`),
        so its cached driver session outlives any single Cassandra outage. A
        session the driver has shut down can never recover, so it is dropped
        and re-fetched from the shared pool instead of being handed back
        forever (#3851).
        """
        from nyxgpt.rag.vectorstore_cassandra import driver_object_is_shutdown

        with self._lock:
            if self._session is not None and driver_object_is_shutdown(self._session):
                log.warning("Session-store Cassandra session was shut down; reconnecting")
                self._session = None
                self._schema_ready = False
            if self._session is None:
                self._session, self._keyspace = self._connect()
            assert self._keyspace is not None
            if not self._schema_ready:
                try:
                    self._ensure_schema(self._session, self._keyspace)
                except Exception as exc:
                    raise SessionStoreError(
                        f"Failed to initialize session store schema: {exc}"
                    ) from exc
                self._schema_ready = True
            return self._session, self._keyspace

    def _table(self, keyspace: str) -> str:
        """Return the fully-qualified sessions table name."""
        return f"{keyspace}.{SESSIONS_TABLE}"

    # -- row helpers ---------------------------------------------------------

    def _fetch_row(self, name: str) -> Any | None:
        """Fetch a session's full row by name, or None when absent."""
        session, ks = self._conn()
        rows = session.execute(
            f"SELECT name, messages, meta, pinned, message_count, updated_at "
            f"FROM {self._table(ks)} WHERE name = %s",
            (name,),
        )
        return rows.one() if hasattr(rows, "one") else next(iter(rows), None)

    # -- public API ----------------------------------------------------------

    def exists(self, name: str) -> bool:
        """Return whether a session exists (its row has messages stored)."""
        row = self._fetch_row(name)
        return row is not None and row.messages is not None

    def load_messages(self, name: str) -> list[dict[str, Any]]:
        """Load and validate a session's messages; [] when absent/invalid."""
        row = self._fetch_row(name)
        if row is None or row.messages is None:
            return []
        try:
            data = json.loads(row.messages)
        except (TypeError, ValueError) as e:
            log.warning("Invalid JSON messages for session %r in Cassandra: %s", name, e)
            return []
        if not isinstance(data, list):
            return []
        return [
            m
            for m in data
            if isinstance(m, dict)
            and isinstance(m.get("role"), str)
            and isinstance(m.get("content"), str)
        ]

    def save_messages(self, name: str, messages: list[dict[str, Any]]) -> None:
        """Upsert a session's messages (row-atomic; last write wins)."""
        session, ks = self._conn()
        session.execute(
            f"INSERT INTO {self._table(ks)} "
            f"(name, messages, message_count, updated_at) VALUES (%s, %s, %s, %s)",
            (name, json.dumps(messages, ensure_ascii=False), len(messages), _iso_now()),
        )

    def load_meta(self, name: str) -> dict[str, Any]:
        """Load a session's metadata dict; {} when absent/invalid."""
        row = self._fetch_row(name)
        if row is None or row.meta is None:
            return {}
        try:
            data = json.loads(row.meta)
        except (TypeError, ValueError) as e:
            log.warning("Invalid JSON metadata for session %r in Cassandra: %s", name, e)
            return {}
        return data if isinstance(data, dict) else {}

    def save_meta(self, name: str, meta: dict[str, Any]) -> None:
        """Upsert a session's metadata (denormalizes pinned/updated_at)."""
        session, ks = self._conn()
        updated_at = meta.get("updated_at") if isinstance(meta.get("updated_at"), str) else None
        session.execute(
            f"INSERT INTO {self._table(ks)} "
            f"(name, meta, pinned, updated_at) VALUES (%s, %s, %s, %s)",
            (
                name,
                json.dumps(meta, ensure_ascii=False),
                bool(meta.get("pinned")),
                updated_at or _iso_now(),
            ),
        )

    def delete(self, name: str) -> bool:
        """Delete a session row. Returns False when the session didn't exist."""
        if not self.exists(name):
            return False
        session, ks = self._conn()
        session.execute(f"DELETE FROM {self._table(ks)} WHERE name = %s", (name,))
        return True

    def rename(self, old: str, new: str) -> tuple[bool, str]:
        """Copy a session row to a new name, then delete the old row."""
        row = self._fetch_row(old)
        if row is None or row.messages is None:
            return False, "No such session"
        if self.exists(new):
            return False, "Target session already exists"
        session, ks = self._conn()
        session.execute(
            f"INSERT INTO {self._table(ks)} "
            f"(name, messages, meta, pinned, message_count, updated_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s)",
            (new, row.messages, row.meta, row.pinned, row.message_count, row.updated_at),
        )
        session.execute(f"DELETE FROM {self._table(ks)} WHERE name = %s", (old,))
        return True, "OK"

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all sessions without loading message bodies, pinned first.

        Returns the same row shape as the file backend's
        ``sessions.list_sessions``: name, file (a ``cassandra://`` locator),
        messages (count), modified, meta.
        """
        session, ks = self._conn()
        rows = session.execute(
            f"SELECT name, meta, pinned, message_count, updated_at, messages "
            f"FROM {self._table(ks)}"
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            if row.messages is None:
                # Metadata-only row (session initialized but never written):
                # the file backend doesn't list these either.
                continue
            try:
                meta = json.loads(row.meta) if row.meta else {}
            except (TypeError, ValueError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            modified = "?"
            if isinstance(row.updated_at, str) and row.updated_at:
                try:
                    modified = datetime.fromisoformat(row.updated_at).strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    modified = row.updated_at
            out.append(
                {
                    "name": row.name,
                    "file": f"cassandra://{ks}/{SESSIONS_TABLE}/{row.name}",
                    "messages": int(row.message_count or 0),
                    "modified": modified,
                    "meta": meta,
                }
            )
        out.sort(key=lambda r: (0 if r["meta"].get("pinned") else 1, str(r["name"]).lower()))
        return out


# -- module-level singleton ---------------------------------------------------

_store_lock = threading.Lock()
_store_instance: CassandraSessionStore | None = None


def get_session_store() -> CassandraSessionStore:
    """Return the module-level :class:`CassandraSessionStore` singleton."""
    global _store_instance
    with _store_lock:
        if _store_instance is None:
            _store_instance = CassandraSessionStore()
        return _store_instance


def reset_session_store() -> None:
    """Clear the singleton (for tests and config reloads)."""
    global _store_instance
    with _store_lock:
        _store_instance = None


# -- one-time migration from the legacy JSON directory ------------------------


def _read_json(path: Path) -> Any | None:
    """Best-effort JSON read used only by the legacy-file migration."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("Migration: cannot read %s: %s", path, e)
        return None


def migrate_sessions_dir(
    sessions_dir: Path, store: CassandraSessionStore | None = None
) -> dict[str, Any]:
    """Import legacy ``<sessions_dir>/*.json`` session files into Cassandra.

    Idempotent by construction: a session whose name already exists in the
    database is never overwritten (re-runs and partially-completed earlier
    runs simply skip what's already there). The source files are left in
    place as a read-only archive; a ``.migrated-to-db.json`` marker with the
    migration report is written into the directory.

    This reads the legacy files directly (not via ``nyxgpt.sessions``): the
    sessions module dispatches to this backend when it is active, so routing
    the migration through it would read from the very store being migrated to.

    Args:
        sessions_dir: The legacy sessions directory to import from.
        store: Store override for tests; defaults to the singleton.

    Returns:
        Report dict: ``{"migrated": [...], "skipped_existing": [...],
        "skipped_invalid": [...], "errors": [...]}``.
    """
    store = store or get_session_store()
    report: dict[str, Any] = {
        "migrated": [],
        "skipped_existing": [],
        "skipped_invalid": [],
        "errors": [],
    }

    if not sessions_dir.is_dir():
        return report

    for path in sorted(sessions_dir.glob("*.json")):
        if path.name.endswith(".meta.json") or path.name == MIGRATION_MARKER:
            continue
        name = path.stem
        if not _VALID_SESSION_NAME.match(name):
            report["skipped_invalid"].append(name)
            continue
        try:
            if store.exists(name):
                report["skipped_existing"].append(name)
                continue
            raw = _read_json(path)
            if not isinstance(raw, list):
                report["skipped_invalid"].append(name)
                continue
            messages = [
                m
                for m in raw
                if isinstance(m, dict)
                and isinstance(m.get("role"), str)
                and isinstance(m.get("content"), str)
            ]
            meta_raw = _read_json(path.with_suffix(".meta.json"))
            meta = meta_raw if isinstance(meta_raw, dict) else {}
            store.save_messages(name, messages)
            if meta:
                store.save_meta(name, meta)
            report["migrated"].append(name)
        except Exception as e:
            log.error("Migration: failed to import session %r: %s", name, e)
            report["errors"].append(name)

    if report["migrated"] or report["errors"]:
        log.info(
            "Session migration: %d imported, %d already in DB, %d invalid, %d errors "
            "(source files kept as archive in %s)",
            len(report["migrated"]),
            len(report["skipped_existing"]),
            len(report["skipped_invalid"]),
            len(report["errors"]),
            sessions_dir,
        )
    try:
        marker = sessions_dir / MIGRATION_MARKER
        marker.write_text(
            json.dumps({"at": _iso_now(), "report": report}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        log.warning("Migration: could not write marker file in %s: %s", sessions_dir, e)

    return report


__all__ = [
    "SESSIONS_TABLE",
    "MIGRATION_MARKER",
    "SessionStoreError",
    "CassandraSessionStore",
    "get_session_store",
    "reset_session_store",
    "migrate_sessions_dir",
]
