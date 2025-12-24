# UI

## Run the backend (FastAPI)

### Verify

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/v1/info
```

Interactive docs (local only):

```bash
open http://127.0.0.1:8000/docs
```

## Sessions API (UI-critical)

- List sessions (returns `{ "sessions": [...] }`):

```bash
curl -s http://127.0.0.1:8000/api/v1/sessions
```

- Initialize a session (no model call; safe for UI bootstrapping; idempotent):
