# nyxGPT web UI

The Next.js application that nyxGPT's **`web` service** serves. It is one half
of a pair: every screen in here talks to the FastAPI backend in
`../src/nyxgpt/`, so this directory is not a standalone app — started on its
own it has no chat endpoints, no sessions and no RAG behind it.

## Running it

The web UI is a managed service, not something you start by hand. Every
lifecycle operation is a `nyxgpt` command:

```bash
nyxgpt up                  # bring the whole stack up on this machine
nyxgpt ops restart web     # restart just this service
nyxgpt ops logs web        # follow its log
nyxgpt ops status          # what is running, and in which install mode
```

To iterate on the code in your checkout, install in **dev mode** — the `web`
service then runs Next's dev server out of this directory, so your edits are
picked up without a rebuild:

```bash
nyxgpt up --dev
```

[`--dev`](../docs/ops.md#--dev-run-the-current-checkout-without-an-artifact-build)
documents what the flag changes for each service;
[docs/development.md](../docs/development.md#local-setup) is the full developer
setup it belongs to.

## Deployment

There is no hosted-platform deployment path here, by design — nyxGPT is
local-first, and nothing it needs lives in someone else's cloud. The supported
targets are listed in
[docs/portability-matrix.md](../docs/portability-matrix.md), and each of them
installs this UI through the same `nyxgpt` commands above: see
[docs/ops.md](../docs/ops.md) (native/Homebrew/systemd),
[docs/docker-compose.md](../docs/docker-compose.md),
[docs/kubernetes.md](../docs/kubernetes.md) and [docs/cloud.md](../docs/cloud.md).

## Tests

[`tests/README.md`](tests/README.md) — the Vitest/MSW harness, how to run it,
and the coverage gate CI blocks on.

## Where the documentation is

- [docs/ui.md](../docs/ui.md) — what this UI surfaces and how it depends on the backend
- [docs/development.md](../docs/development.md) — developer setup, tests, lint
- [docs/ops.md](../docs/ops.md) — the `nyxgpt ops` command reference
- [docs/api.md](../docs/api.md) — the backend these screens call
- [docs/service-worker-pwa.md](../docs/service-worker-pwa.md) — offline and PWA behaviour
- [docs/README.md](../docs/README.md) — the complete documentation index
