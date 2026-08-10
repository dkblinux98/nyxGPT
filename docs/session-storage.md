# Session Storage Backends

Chat sessions can be stored in one of two backends, selected by
`[nyxgpt] session_backend` in `config.ini` (or the `NYXGPT_SESSION_BACKEND`
environment variable, which takes precedence):

| Backend | Where sessions live | Suitable for |
|---|---|---|
| `file` (default) | One `<name>.json` + `<name>.meta.json` pair per session under `[nyxgpt] sessions_dir` (default `~/.nyxGPT/sessions`) | Single-host native mode only |
| `cassandra` | The `chat_sessions` table in the stack's Cassandra (the same instance and keyspace the RAG store uses, configured by `[rag] cassandra_*`) | Every deployment mode; required for Compose/Terraform/Kubernetes and any multi-instance setup |

## Why the database backend exists (#3590)

With the file backend, every non-native deployment mode gets its own disk and
therefore its own disjoint session list: the Terraform-managed API container's
volume, the Compose volume, a Kubernetes pod's filesystem, and the host's
`~/.nyxGPT/sessions` are four different session stores. This was observed live
in Terraform mode (2026-08-02): the sessions sidebar listed stale sessions
from a previous Terraform volume instead of the host's sessions.

With `session_backend = cassandra`, all modes read and write the same
Cassandra table. `nyxgpt ops install`/`env-sync` derive the containerized
API's config verbatim from the native `~/.nyxGPT/config.ini` (rewriting only
service hostnames, e.g. `cassandra_hosts = cassandra`), and both point at the
same `nyxgpt-cassandra` instance — so setting the backend once in the native
config makes every mode share one session list *by construction*. Cassandra
writes are atomic per partition (one row per session), so concurrent writes
from multiple API instances (canary + stable, horizontally scaled replicas)
can never produce a torn session document; the last complete write wins,
which is the same policy the file store's atomic rename gave a single host.

## Enabling it

```ini
[nyxgpt]
session_backend = cassandra
```

Cassandra is already a required core service (`nyxgpt ops install` provisions
`nyxgpt-cassandra`); no new infrastructure is needed. The keyspace and
`chat_sessions` table are created automatically on first use.

## Schema

In the `[rag] cassandra_keyspace` keyspace (default `nyxgpt`):

```cql
CREATE TABLE chat_sessions (
    name          text PRIMARY KEY,  -- validated session name
    messages      text,              -- JSON array, same shape as <name>.json
    meta          text,              -- JSON object, same shape as <name>.meta.json
    pinned        boolean,           -- denormalized for cheap list sorting
    message_count int,               -- denormalized so listing never loads messages
    updated_at    text               -- ISO 8601
);
```

Listing sessions reads only the metadata columns (never the message bodies),
so the session list stays fast at hundreds of sessions — with the file
backend, listing had to open two files per session.

## One-time migration of existing file sessions

When the API starts with `session_backend = cassandra`, it imports any legacy
`sessions_dir/*.json` sessions into the database:

- **Idempotent:** a session name already present in the database is never
  overwritten — re-runs and interrupted runs simply skip it.
- **Logged:** startup logs report how many sessions were imported, already
  present, invalid, or failed; a `.migrated-to-db.json` marker file with the
  full report is written into the sessions directory.
- **Archive:** the JSON files are **kept in place as a read-only archive**
  (documented decision — they are never deleted or modified, and nothing
  reads them again except a future migration pass). Delete the directory
  yourself if and when you no longer want the archive.

## `sessions_dir` deprecation and back-compat

- `[nyxgpt] sessions_dir` remains fully supported for the `file` backend
  (the default), which stays appropriate for a single-host native install.
- Under the `cassandra` backend, `sessions_dir` is **deprecated**: it is only
  read as the one-time migration source. The `sessions_dir` query-parameter
  override on the `/api/v1/sessions*` endpoints is likewise ignored under the
  DB backend (there is exactly one shared store).
- **Export for backup/portability is preserved:** `GET
  /sessions/{name}/export` and the CLI export commands (Markdown/JSON/HTML)
  work identically under both backends, so file-level backups remain an
  explicit export away.
- Switching back to `session_backend = file` is possible at any time;
  sessions created while on the DB backend are not synced back to files
  (export the ones you need first).

## Interim bind-mount stopgap: evaluated and rejected

Before this landed, an interim option was considered: bind-mounting the
host's `~/.nyxGPT/sessions` into the Terraform/Compose API containers so all
modes share the same files. **Decision: not adopted.** Reasons:

1. It couples containers to a host path, which the repo-less portability
   requirement (owner, 2026-08-01) explicitly retires — cloud modes (EC2,
   k8s) have no host directory to mount, so it could never cover all modes.
2. Concurrent access from multiple instances to the same JSON files relies
   on host-local advisory file locks that do not work across container
   boundaries or hosts, risking corruption — the exact class of problem the
   DB backend removes.
3. The DB backend supersedes it in the same release, so a stopgap would have
   shipped only migration debt.

## Testing

`tests/unit/test_session_db.py` covers the store CRUD/list/pin/rename paths,
the migration (fresh, partial, re-run), the `nyxgpt.sessions` dispatch layer,
and the multi-writer concurrency guarantee, all against an in-memory fake of
the Cassandra driver session (no live Cassandra needed).
