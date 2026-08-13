# nyxGPT

**nyxGPT** is a local-first, private, extensible ChatGPT-style system designed
to run entirely on your own machine: **Ollama** for local LLM inference,
persistent conversation sessions, optional **Retrieval-Augmented Generation
(RAG)** backed by **Apache Cassandra**, a **CLI**, a **FastAPI** backend, and a
lightweight **Next.js** web UI — with production-style operations (metrics,
dashboards, logs, traces, canary deployment, self-healing) behind
`nyxgpt`-wrapped commands.

Your data stays on your machine. No cloud dependency is required.

---

## What nyxGPT actually is

The chat/RAG application above is real and working, but the code is not the
point — it's the vehicle. **nyxGPT is a reference implementation of
full-lifecycle software delivery discipline** — observability, canary
deployment, self-healing, release management, and an agent-run delivery
process — held together at a scale small enough for one person to read
end-to-end and check every claim against the actual commit and issue history.

**This is an agent-coded project — AI agents wrote the overwhelming majority of
the code — managed by a person with 25 years of SRE and Release Management
experience**, from individual-contributor through Director-level leadership
roles, in medium-size startups and mature organizations. That background is the
explanation for the project's shape, not a bio aside: it is what running an
engineering organization staffed by AI agents, part-time, looks like when the
person building the guardrails has spent a career running release trains and
on-call rotations.

Work is specified precisely enough to delegate; gates exist because delegated
work fails, and are built to catch it; and release discipline holds even though
no one reviews every line. **Agent failures are expected and routine — it is
the surrounding process, not agent infallibility, that turns that into
production-grade output.**

See **[How this project is run](https://github.com/dkblinux98/nyxGPT/blob/master/docs/how-this-project-is-run.md)**
for the mechanics: the agent roles and their charters/runbooks, the status flow
from Backlog to release, the decision-record practice, the Definition of Done,
and the retrospective that shows those gates firing.

---

## Install

```bash
pip install nyxgpt   # published wheel; no repository checkout required
nyxgpt up            # installs and starts every component, then prints the web UI URL
```

Then chat from the CLI (`nyxgpt chat "Hello"`) or the web UI at the URL
`nyxgpt up` prints.

- **macOS native services** install from the remote Homebrew tap instead —
  [Homebrew](https://github.com/dkblinux98/nyxGPT/blob/master/docs/homebrew.md).
- **Every supported target** (macOS, Linux, Docker/Compose, Kubernetes, AWS
  EC2), its exact install command, and its current state —
  [Portability matrix](https://github.com/dkblinux98/nyxGPT/blob/master/docs/portability-matrix.md).
- **First run and configuration** (`nyxgpt wizard`, `nyxgpt secrets setup`,
  `~/.nyxGPT/config.ini`) —
  [Installing nyxGPT](https://github.com/dkblinux98/nyxGPT/blob/master/docs/ops.md#installing-nyxgpt)
  and
  [Configuration](https://github.com/dkblinux98/nyxGPT/blob/master/docs/configuration.md).
- **Acceptance-testing a build that isn't released yet** — install a pinned
  release candidate rather than the latest stable:
  [PyPI publishing (rc and stable)](https://github.com/dkblinux98/nyxGPT/blob/master/docs/cloud.md#pypi-publishing-rc-and-stable).
- **Developing nyxGPT** is the only flow that uses a source checkout
  (`pip install -e .`) —
  [Development](https://github.com/dkblinux98/nyxGPT/blob/master/docs/development.md).

Which versions exist at any given moment is on the
[PyPI project page](https://pypi.org/project/nyxgpt/); this file does not
restate it.

---

## Documentation

Everything else — features, commands, deployment paths, architecture, and the
agent system — lives under `docs/`. Start at the
**[documentation index](https://github.com/dkblinux98/nyxGPT/blob/master/docs/README.md)**,
which is the complete, grouped list (Feature overview · User guides ·
Operations & deployment · Developer · Agent system).

**If you have nyxGPT installed, you already have these docs.** The same tree
ships inside the package, so the web UI serves it under **Support → Docs** —
no checkout, no internet. The Support menu's other item, **File an Issue**,
opens a report form with your version and platform filled in (that one needs
internet and a GitHub account).

Common starting points:

| | |
|---|---|
| [Configuration](https://github.com/dkblinux98/nyxGPT/blob/master/docs/configuration.md) | every `config.ini` section and key |
| [CLI](https://github.com/dkblinux98/nyxGPT/blob/master/docs/cli.md) | the full `nyxgpt` command reference |
| [Ops helpers](https://github.com/dkblinux98/nyxGPT/blob/master/docs/ops.md) | install, service lifecycle, diagnostics |
| [API](https://github.com/dkblinux98/nyxGPT/blob/master/docs/api.md) | REST reference (`/api/v1/*`) |
| [UI](https://github.com/dkblinux98/nyxGPT/blob/master/docs/ui.md) | the web interface |
| [RAG](https://github.com/dkblinux98/nyxGPT/blob/master/docs/rag.md) | ingestion, collections, querying |
| [Architecture](https://github.com/dkblinux98/nyxGPT/blob/master/docs/architecture.md) | system design, boundaries, invariants |
| [Troubleshooting](https://github.com/dkblinux98/nyxGPT/blob/master/docs/troubleshooting.md) | common problems and fixes |

New to the project: **configuration**, then **architecture**, then **api**.

---

## License

nyxGPT is released under the
[MIT License](https://github.com/dkblinux98/nyxGPT/blob/master/LICENSE).
