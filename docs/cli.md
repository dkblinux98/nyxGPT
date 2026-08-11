# CLI Reference

The `nyxgpt` command is the primary interface for local use. This page
covers the general-purpose commands; command groups with their own deep
documentation are linked out to instead of duplicated here:

- Sessions (`nyxgpt sessions ...`) — see [Sessions](sessions.md)
- RAG (`nyxgpt rag ...`) — see [RAG](rag.md)
- Bring the stack up/down (`nyxgpt up` / `nyxgpt down`) — thin aliases for
  `nyxgpt ops install`/`nyxgpt ops down`, see
  [Ops helpers](ops.md#nyxgpt-up--nyxgpt-down)
- Ops (`nyxgpt ops ...`) — see [Ops helpers](ops.md)
- Cloud/AWS (`nyxgpt cloud ...`) — deploy, tunnel, destroy, the end-to-end
  `nyxgpt cloud smoke` test, Terraform state, and credentials setup; see
  [Cloud (AWS)](cloud.md)
- Canary (`nyxgpt canary ...`) — see [Kubernetes](kubernetes.md)
- Configuration wizard (`nyxgpt wizard`) — see [Configuration](configuration.md)
- Guided secrets setup (`nyxgpt secrets setup`) — see [Configuration: Guided secrets setup](configuration.md#option-4-guided-secrets-setup)
- PyPI builds — dev/rc/stable (`nyxgpt release publish`) — see
  [Cloud: PyPI publishing](cloud.md#pypi-publishing-dev-rc-and-stable)

---

## Info

`nyxgpt info` is the default command when run with no arguments:

```bash
nyxgpt info
```

Prints config-derived defaults (Ollama base URL, default model).

---

## Chat

```bash
nyxgpt chat "Hello"
```

Continue or create a named session with `--session`, and force RAG on for a
single request with `--rag-mode` — see [Sessions — CLI usage](sessions.md#cli-usage).

---

## Model Management

```bash
# List available models
nyxgpt models list

# Pull (download) a model
nyxgpt models pull llama3.1:8b

# Delete a model
nyxgpt models delete mistral:7b

# Show detailed model information
nyxgpt models show llama3.1:8b
```

These map to the `/api/v1/models*` endpoints — see
[API — Models endpoints](api.md#models-endpoints).

---

## Message Search

```bash
# Search across all sessions for message content
nyxgpt sessions search "Python programming"

# Case-sensitive search
nyxgpt sessions search "Python" --case-sensitive

# Filter by role (user, assistant, or system)
nyxgpt sessions search "error" --role user

# Search within a specific session
nyxgpt sessions search "database" specific-session-name

# Limit number of results
nyxgpt sessions search "test" --limit 10
```

The search command finds messages containing the query text and displays
the session name/title, message index and role, number of matches per
message, and a content preview with surrounding context. See
[Sessions — Search sessions](sessions.md#search-sessions) for the full
reference (including the equivalent `GET /api/v1/sessions/search` API).

---

## PyPI builds — dev, rc, stable (owner)

`nyxgpt release publish` plans — and with `--publish`, cuts — a PyPI build
from the release-branch tip, so acceptance testing can install unreleased
code on a machine with no repo checkout. One pipeline serves all three
channels; the nightly `dev` build runs on a schedule with no command at all.

```bash
# What would be published, and whether it can be cut from here.
nyxgpt release publish

# Machine-readable (the same payload GET /api/v1/ops/release-candidate returns).
nyxgpt release publish --json

# Dispatch the publish workflow on the release branch.
nyxgpt release publish --publish

# An immediate dev build instead of waiting for tonight's schedule.
nyxgpt release publish --channel dev --publish

# `nyxgpt release rc` is shorthand for `--channel rc` (with `--rc-number`).
nyxgpt release rc --publish --rc-number 4
```

| Flag | Description |
| --- | --- |
| `--branch <branch>` | Release branch the build is cut from. Default: `[github] RELEASE_BRANCH` from `config.ini`, else `v<declared version>`. Non-release branches are refused. |
| `--channel <dev\|rc>` | Which channel to plan or publish (default `rc`). `stable` is published only by `scripts/release_ceremony.sh`, which delegates to the same workflow. |
| `--publish` | Dispatch the publish workflow instead of only reporting what it would do. |
| `--number <n>` | Publish a specific rc/dev number instead of the next unused one. |
| `--json` | Print the plan as JSON. |

Exits non-zero when a build cannot be cut from the given branch, so it can
gate a script. What `dev` and `rc` publish is always a PEP 440 pre-release
(`3.0.0.devN`, `3.0.0rcN`), which `pip install nyxgpt` never resolves to.

Cutting an `rc` additionally stamps `nyxgpt-api@rc` / `nyxgpt-web@rc` into
the Homebrew tap (and cuts a GitHub prerelease with the service tarballs),
so a candidate is installable on macOS too:

```bash
brew tap dkblinux98/nyxgpt && brew install nyxgpt-api@rc nyxgpt-web@rc
```

The stable `nyxgpt-api`/`nyxgpt-web` formulas are never written by an `rc`
or `dev` publish — see [docs/homebrew.md](homebrew.md#release-candidate-formulas-rc).
A dispatched `--channel dev --publish` publishes nothing when the tip has
not moved since the last nightly (that commit already has a dev build); the
run reports `SKIP` rather than failing.
Full runbook, including the one-time PyPI Trusted Publishing setup:
[Cloud — PyPI publishing](cloud.md#pypi-publishing-dev-rc-and-stable).

