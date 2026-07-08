# Docker Compose (full stack)

`docker-compose.yml` brings up every component of nyxGPT with one command:
the FastAPI backend, the Next.js web UI, Ollama, and Cassandra (for RAG).
This is an alternative to the Homebrew / `nyxgpt ops` workflow described in
[ops.md](ops.md) and the API-only [Kubernetes deployment](kubernetes.md) —
useful when you want the whole stack running in containers with a single
command, e.g. for evaluation or a non-macOS host.

## Services

| Service     | Image             | Purpose                                   | Host port (default) |
|-------------|-------------------|--------------------------------------------|----------------------|
| `ollama`    | `ollama/ollama`   | Local LLM inference                        | `11434`              |
| `cassandra` | `cassandra:5.0`   | Vector store for RAG                       | `9042`               |
| `api`       | built from `Dockerfile`     | FastAPI backend (`nyxgpt-api`)  | `8000`               |
| `web`       | built from `web/Dockerfile` | Next.js web UI (`nyxgpt-web`)   | `3000`               |

All services share a single bridge network (`nyxgpt`) and reach each other by
service name (e.g. the API talks to Ollama at `http://ollama:11434` and to
Cassandra at `cassandra:9042` — see `docker/config.docker.ini`).

## Volumes

Three named volumes persist state across `docker compose down` / `up`:

- `ollama_data` — pulled models (`/root/.ollama`)
- `cassandra_data` — Cassandra's data directory (`/var/lib/cassandra`)
- `nyxgpt_data` — chat sessions, vector store, and logs (`/root/.nyxGPT` in
  the `api` container)

Run `docker compose down -v` to discard all persisted state, including
Cassandra data and pulled models.

## Quickstart

```bash
cp .env.example .env
# edit .env and set a real NYXGPT_AUTH_API_KEY

docker compose up --build
```

First boot takes a few minutes: Cassandra needs time to become healthy and
Ollama needs a model. Once `ollama` is up, pull the default model (this only
needs to be done once — it's stored in the `ollama_data` volume):

```bash
docker compose exec ollama ollama pull qwen2.5:0.5b
```

Then verify:

```bash
curl -H "X-API-Key: <NYXGPT_AUTH_API_KEY from .env>" http://127.0.0.1:8000/health
```

Open the web UI at [http://localhost:3000](http://localhost:3000).

## Configuration

- **API config**: `docker/config.docker.ini` is mounted read-only into the
  `api` container at `/etc/nyxgpt/config/config.ini` and copied to
  `~/.nyxGPT/config.ini` by `docker/entrypoint.sh` on startup, which also
  merges in `NYXGPT_AUTH_API_KEY`. Edit this file for persistent settings
  (default model, RAG thresholds, etc.) — see [configuration.md](configuration.md)
  for the full option reference.
- **Environment**: all other tunables (ports, the API key, CORS origins, the
  web UI's API base URL, image tags) are set via `.env` — see
  `.env.example` for the full list.
- **Web UI → API URL**: `NEXT_PUBLIC_API_BASE_URL` is inlined into the web
  UI's client bundle at *build* time (Next.js `NEXT_PUBLIC_*` semantics), so
  changing it requires rebuilding: `docker compose build web`.

## Disabling RAG / Cassandra

RAG is enabled by default in `docker/config.docker.ini` since Cassandra ships
as part of this stack. To run without RAG, set `enable_chat_context = false`
under `[rag]` in `docker/config.docker.ini`; you can also remove the
`cassandra` service and its `depends_on` entry under `api` in
`docker-compose.yml` if you don't want the container running at all.

## Rebuilding after code changes

```bash
docker compose build api web
docker compose up -d
```
