# CLI Reference

The `nyxgpt` command is the primary interface for local use. This page
covers the general-purpose commands; command groups with their own deep
documentation are linked out to instead of duplicated here:

- Sessions (`nyxgpt sessions ...`) — see [Sessions](sessions.md)
- RAG (`nyxgpt rag ...`) — see [RAG](rag.md)
- Bring the stack up/down (`nyxgpt up` / `nyxgpt down`) — thin aliases for
  `nyxgpt ops install`/`nyxgpt ops down`, see
  [Ops helpers](ops.md#nyxgpt-up--nyxgpt-down). `nyxgpt up --dev` brings the
  same stack up from the current checkout instead of from artifacts — see
  [`--dev`](ops.md#--dev-run-the-current-checkout-without-an-artifact-build)
- Ops (`nyxgpt ops ...`) — see [Ops helpers](ops.md)
- Cloud/AWS (`nyxgpt cloud ...`) — deploy, `nyxgpt cloud status` (what is
  deployed and how to reach it), `nyxgpt cloud ops` (read-only inspections
  run on the instance), tunnel, destroy, the end-to-end `nyxgpt cloud smoke`
  test, Terraform state, and credentials setup; see [Cloud (AWS)](cloud.md)
- Cloud artifact smoke (`nyxgpt cloud smoke --container`) — the artifact
  install path on a bare Amazon Linux 2023 container, no AWS account and no
  charges; see [Cloud artifact smoke](cloud-artifact-smoke.md)
- Canary (`nyxgpt canary ...`) — see [Kubernetes](kubernetes.md)
- Configuration wizard (`nyxgpt wizard`) — see [Configuration](configuration.md)
- Guided secrets setup (`nyxgpt secrets setup`) — see [Configuration: Guided secrets setup](configuration.md#option-4-guided-secrets-setup)
- PyPI builds — rc/stable (`nyxgpt release publish`) — see
  [Cloud: PyPI publishing](cloud.md#pypi-publishing-rc-and-stable)

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

## PyPI builds — rc, stable (owner)

`nyxgpt release publish` plans — and with `--publish`, cuts — a PyPI build
from the release-branch tip, so acceptance testing can install unreleased
code on a machine with no repo checkout. One pipeline serves both channels,
and nothing is published on a schedule — most rounds need no command at all,
because the sprint autopilot cuts the candidate at agentic-work-complete.

```bash
# What would be published, and whether it can be cut from here.
nyxgpt release publish

# Machine-readable (the same payload GET /api/v1/ops/release-candidate returns).
nyxgpt release publish --json

# Dispatch the publish workflow on the release branch.
nyxgpt release publish --publish

# `nyxgpt release rc` is shorthand for `--channel rc` (with `--rc-number`).
nyxgpt release rc --publish --rc-number 4
```

| Flag | Description |
| --- | --- |
| `--branch <branch>` | Release branch the build is cut from. Default: `[github] RELEASE_BRANCH` from `config.ini`, else `v<declared version>`. Non-release branches are refused. |
| `--channel rc` | Which channel to plan or publish (default and only choice: `rc`). `stable` is published only by `scripts/release_ceremony.sh`, which delegates to the same workflow. |
| `--publish` | Dispatch the publish workflow instead of only reporting what it would do. |
| `--number <n>` | Publish a specific rc number instead of the next unused one. |
| `--json` | Print the plan as JSON. |

Exits non-zero when a build cannot be cut from the given branch, so it can
gate a script. What `rc` publishes is always a PEP 440 pre-release
(`3.0.0rcN`), which `pip install nyxgpt` never resolves to.

Cutting an `rc` additionally stamps `nyxgpt-api@<release>rc` /
`nyxgpt-web@<release>rc` into the Homebrew tap (and cuts a GitHub prerelease
with the service tarballs), so a candidate is installable on macOS too:

```bash
brew tap dkblinux98/nyxgpt
brew tap-trust dkblinux98/nyxgpt   # one-time per machine (docs/homebrew.md)
brew install nyxgpt-api@3.0.0rc nyxgpt-web@3.0.0rc
```

The formula name carries the release line, so a candidate never crosses to
the next one. The stable `nyxgpt-api`/`nyxgpt-web` formulas are never
written by an `rc` publish — see
[docs/homebrew.md](homebrew.md#release-candidate-formulas-rc-channel).
A `--publish` on a tip that has not moved since the last published candidate
publishes nothing (that commit already has one); the run reports `SKIP`
rather than failing. Pass `--number` to override.
Full runbook, including the one-time PyPI Trusted Publishing setup:
[Cloud — PyPI publishing](cloud.md#pypi-publishing-rc-and-stable).

