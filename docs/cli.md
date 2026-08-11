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
- Release candidates (`nyxgpt release rc`) — see
  [Cloud: Release candidates](cloud.md#release-candidates-acceptance-testing-unreleased-code)

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

## Release candidates (owner)

`nyxgpt release rc` plans — and with `--publish`, cuts — a PyPI release
candidate from the release-branch tip, so acceptance testing can install
unreleased code on a machine with no repo checkout.

```bash
# What would be published, and whether it can be cut from here.
nyxgpt release rc

# Machine-readable (the same payload GET /api/v1/ops/release-candidate returns).
nyxgpt release rc --json

# Dispatch the publish workflow on the release branch.
nyxgpt release rc --publish
```

| Flag | Description |
| --- | --- |
| `--branch <branch>` | Release branch the RC is cut from. Default: `[github] RELEASE_BRANCH` from `config.ini`, else `v<declared version>`. Non-release branches are refused. |
| `--publish` | Dispatch the publish workflow instead of only reporting what it would do. |
| `--rc-number <n>` | Publish a specific RC number instead of the next unused one. |
| `--json` | Print the plan as JSON. |

Exits non-zero when an RC cannot be cut from the given branch, so it can gate
a script. What gets published is always a PEP 440 pre-release (`3.0.0rcN`),
which `pip install nyxgpt` never resolves to. Full runbook, including the
one-time PyPI credential setup:
[Cloud — Release candidates](cloud.md#release-candidates-acceptance-testing-unreleased-code).
