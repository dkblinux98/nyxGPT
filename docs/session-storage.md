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

## How each deployment mode selects the backend (#3865)

Every mode resolves the backend through the same two inputs
(`config.get_session_backend`): the `NYXGPT_SESSION_BACKEND` environment
variable if set, else `[nyxgpt] session_backend` in that process's
`config.ini`. What differs per mode is *who writes that value*:

| Mode | Default | How it is selected | Sharing |
|---|---|---|---|
| Native (local, single host) | `file` | `example.config.ini`'s shipped value; change it with `nyxgpt ops session-backend cassandra` | The other modes on this host share the value, because they derive their config from this one |
| Docker Compose | inherited | `nyxgpt ops install`/`env-sync` derives `docker/config.docker.ini` **verbatim** from the native `config.ini`, rewriting only service hostnames (`cassandra_hosts = cassandra`) | Shares the native host's backend by construction — set it once natively and Compose follows |
| Terraform, local containers | inherited | The same derived `config.docker.ini`; the Terraform containers get matching network aliases | As Compose |
| Kubernetes | `cassandra` | Declarative in `k8s/configmap.yaml` (`session_backend = cassandra`); overridable per pod with `NYXGPT_SESSION_BACKEND` | Every api replica reads and writes one store — required, since replicas have no shared disk |
| Cloud — `nyxgpt cloud deploy --os linux` | `cassandra` | `--session-backend {file,cassandra}`, applied on the instance by the provisioning script before `ops install`, and recorded in `deploy.json` so a re-deploy keeps it | Shares with any other mode pointed at that Cassandra |
| Cloud — `nyxgpt cloud deploy --os macos` (EC2 Mac) | `file` | `--session-backend`, applied by the same bootstrap the deploy delivers over SSH (#3867), and recorded in `deploy.json` per target OS — a re-deploy that switches OS family takes the new family's default rather than inheriting the other's | **File-backed by default, deliberately.** That bootstrap installs the two Homebrew formulas and starts them; it does not run `ops install`, so nothing provisions a Cassandra on the instance. Passing `--session-backend cassandra` is supported for an operator who points `[rag] cassandra_hosts` at a Cassandra they run elsewhere |
| Cloud — `nyxgpt cloud user-data --os {linux,macos}` | as the matching deploy row | `--session-backend`, applied by the rendered bootstrap before the services start | The renderer behind the deploy rows above; same defaults, same mechanism |

The cloud rows are the ones that changed in #3865. Before it, the cloud paths
seeded `config.ini` from `example.config.ini` and never touched the backend,
so a provisioned instance silently ran `file`: chats were JSON files on
ephemeral instance disk, invisible to every other mode pointed at the same
Cassandra, and lost with the instance. Changing it meant an SSH session and a
hand edit of `config.ini` — the raw-operations flow the wrapped-command
requirement forbids as the user-facing path.

## Enabling it

The wrapped command, on any host or instance:

```bash
nyxgpt ops session-backend cassandra   # set it
nyxgpt ops session-backend             # report the backend in force
nyxgpt ops restart api                 # the API reads it at startup
```

It edits `[nyxgpt] session_backend` in `config.ini` in place, leaving the
file's comments intact, and writes nothing when the value is already what you
asked for — so a provisioning script can call it on every run. With no
argument it reports the value actually in force, including an
`NYXGPT_SESSION_BACKEND` override of the file, which is what you want when a
container disagrees with its config.

Or set it directly:

```ini
[nyxgpt]
session_backend = cassandra
```

Cassandra is already a required core service (`nyxgpt ops install` provisions
`nyxgpt-cassandra`); no new infrastructure is needed. The keyspace and
`chat_sessions` table are created automatically on first use.

For a cloud deployment, select it at deploy time instead — no SSH, no
instance edit:

```bash
nyxgpt cloud deploy --session-backend cassandra   # the default
nyxgpt cloud deploy --session-backend file        # host-local JSON sessions
```

`nyxgpt cloud status` and the admin dashboard's Infrastructure page report
which backend the deployment recorded; `nyxgpt cloud ops session-backend`
asks the instance itself over the same wrapped SSH path.

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

That the *cloud* path actually ends up on this backend is proven by running
it, not by inspection (#3865): the `session-backend` phase of
`nyxgpt cloud smoke --container`
(`.github/workflows/cloud-artifact-smoke.yml`) brings a bare Amazon Linux
2023 machine up through the real rendered EC2 bootstrap, then asks the
*running API* to create a session over HTTP and reads that row back out of
the instance's own Cassandra with `cqlsh`. A companion job runs the same
install with `--inject file-sessions` — the session-backend wiring removed —
and passes only if the smoke fails; without it, a check that had stopped
looking would stay green. This defect class is invisible to inspection
precisely because the instance is *healthy* while it happens.
