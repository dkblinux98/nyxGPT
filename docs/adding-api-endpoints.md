# Adding New API Endpoints

This guide explains how to properly add new API endpoints to nyxGPT.

## Architecture Overview

nyxGPT uses a three-tier API architecture:

```
Frontend (React/Next.js)
    ↓ fetch('/api/v1/...')
Web Proxy (Next.js API Routes)
    ↓ fetch('http://localhost:8000/api/v1/...')
Backend (FastAPI)
    ↓
Database/Services
```

## Steps to Add a New Endpoint

### 1. Add Backend Endpoint (FastAPI)

**File**: `src/nyxgpt/app.py`

```python
@api.get("/your-feature/action")
def your_endpoint(request: Request) -> dict:
    """Endpoint description."""
    # Implementation
    return {"result": "data"}
```

**Important**: The `@api` router is prefixed with `/api/v1`, so this creates `/api/v1/your-feature/action`.

### 2. Restart Backend Service

The backend needs to be restarted to register new endpoints:

```bash
nyxgpt ops restart api
```

**Common mistake**: Forgetting this step causes 404 errors even though the code exists.

### 3. Create Web Proxy Route (Next.js)

**File**: `web/src/app/api/v1/your-feature/action/route.ts`

**Always** reach the backend through the shared `apiFetch()` helper
(`web/src/lib/apiProxy.ts`) instead of calling `fetch()` directly with a
hand-rolled base URL. The helper resolves the base URL from the one
canonical env var (`NYXGPT_API_BASE_URL`) and attaches the `X-API-Key` auth
header (from `NYXGPT_AUTH_API_KEY`) on every call — hand-rolling either of
those per route is exactly how #3178 (web UI couldn't reach or authenticate
to the API) happened.

**Exception:** `web/src/app/api/chat/stream/route.ts` does not use `apiFetch()`.
Next 16 bundles its own (newer-major) `undici` for global `fetch`, and handing
that fetch a dispatcher/Agent built from the `undici` version pinned in
`web/package.json` breaks with `UND_ERR_INVALID_ARG` (#3440) — the two undici
majors disagree on the dispatch-handler interface. That route instead calls
the pinned `undici` package's own `request()` directly (still resolving the
base URL via `apiUrl()` and attaching auth via `attachApiKey()`, both from
`@/lib/apiProxy`), so its HTTP client and its options come from the same
undici major regardless of what Next/Node bundle for global fetch. Do not
"fix" it back to `apiFetch()`/`fetch()` with a custom dispatcher.

```typescript
import { apiFetch } from "@/lib/apiProxy";

export async function GET() {
  try {
    const res = await apiFetch("/api/v1/your-feature/action", {
      method: "GET",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });

    if (!res.ok) {
      return new Response(
        JSON.stringify({ error: `Backend returned ${res.status}` }),
        { status: res.status, headers: { "Content-Type": "application/json" } }
      );
    }

    const data = await res.json();
    return new Response(JSON.stringify(data), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    console.error("Failed to fetch from backend:", error);
    return new Response(
      JSON.stringify({ error: "Failed to fetch from backend" }),
      { status: 502, headers: { "Content-Type": "application/json" } }
    );
  }
}
```

For POST endpoints, add:

```typescript
import { apiFetch } from "@/lib/apiProxy";

export async function POST(request: Request) {
  const body = await request.json();

  try {
    const res = await apiFetch("/api/v1/your-feature/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });

    // ... same error handling as GET
  } catch (error) {
    // ... same error handling
  }
}
```

`web/tests/app/api/proxy-routes-canonical-base-url.test.ts` fails the build
if a route calls `fetch()` without going through `apiFetch()`, or references
a non-canonical base-URL env var (`NEXT_PUBLIC_API_URL`, `NYXGPT_API_BASE`)
or a hardcoded backend host — run it after adding a route.

### 4. Restart Web Service

```bash
nyxgpt ops restart web
```

### 5. Use in Frontend

**File**: `web/src/app/components/YourComponent.tsx`

```typescript
import { apiErrorText, errorMessage } from '@/lib/apiError';

async function fetchData() {
  try {
    const res = await fetch('/api/v1/your-feature/action');
    const data = await res.json();
    if (!res.ok) {
      // Never `data.error` / `data.detail` directly -- both can be objects.
      throw new Error(apiErrorText(data, `HTTP ${res.status}`));
    }
    // Use data
  } catch (err) {
    setError(errorMessage(err));
  }
}
```

**Always read a failed response through `web/src/lib/apiError.ts`.** Every
`HTTPException` leaves the backend in the envelope
`{"error": {"code", "message", "details", "request_id"}}` (see
`http_exception_handler` in `src/nyxgpt/app.py`), and FastAPI's own refusals
use `{"detail": ...}` where `detail` may be a string, a list of validation
errors, or a dict. Interpolating any of those into `new Error()` renders the
UI as `[object Object]` and hides the actual failure — that was #3831, where
it hid a Pod scheduling failure from the operator. `apiErrorText` unwraps
every one of those shapes and falls back to the status line; `errorMessage`
does the same for a value caught in a `catch`.

## Validation

Run the validation script to check for missing routes:

```bash
./scripts/agents/validate-web-routes.sh
```

This script:
- Scans all `fetch()` calls in the frontend
- Checks if corresponding web proxy routes exist
- Reports missing routes

## Common Issues

### Issue: 404 Not Found

**Symptoms**: Frontend gets 404 when calling API

**Causes**:
1. ✅ Backend endpoint not restarted → Run `nyxgpt ops restart api`
2. ✅ Web proxy route doesn't exist → Create `route.ts` file
3. ✅ Web service not restarted → Run `nyxgpt ops restart web`
4. ✅ Typo in path → Double-check path matches exactly

### Issue: "No documents available" in UI

**Symptoms**: UI shows "No documents available" but data exists

**Root cause**: Missing web proxy route

**Solution**: Create the missing `route.ts` file in `web/src/app/api/v1/...`

### Issue: CORS errors

**Symptoms**: Browser shows CORS policy errors

**Why**: Direct frontend → backend calls bypass the web proxy

**Solution**: Always use `/api/v1/...` paths (web proxy), never `http://localhost:8000/...` (direct backend)

## Best Practices

1. **Always create web proxy routes** for endpoints called from frontend
2. **Restart services** after adding endpoints
3. **Run validation script** before committing: `./scripts/agents/validate-web-routes.sh`
4. **Use TypeScript types** for request/response (create in `web/src/types/`)
5. **Add error handling** in web proxy routes
6. **Document new endpoints** in this file or OpenAPI schema

## Testing

### Test Backend Endpoint Directly

```bash
curl http://localhost:8000/api/v1/your-feature/action
```

### Test Web Proxy

```bash
curl http://localhost:3000/api/v1/your-feature/action
```

### Check OpenAPI Schema

```bash
curl http://localhost:8000/openapi.json | jq '.paths | keys'
```

## Troubleshooting Checklist

When adding a new endpoint, verify:

- [ ] Backend endpoint added to `src/nyxgpt/app.py`
- [ ] Backend service restarted (`nyxgpt ops restart api`)
- [ ] Endpoint appears in OpenAPI schema
- [ ] Web proxy route created at `web/src/app/api/v1/.../route.ts`
- [ ] Web service restarted (`nyxgpt ops restart web`)
- [ ] Validation script passes (`./scripts/agents/validate-web-routes.sh`)
- [ ] Frontend can fetch from `/api/v1/...` successfully

## Example: Full Implementation

See the RAG documents endpoint for a complete example:

- **Backend**: `src/nyxgpt/app.py:1614` - `@api.get("/rag/documents")`
- **Web Proxy**: `web/src/app/api/v1/rag/documents/route.ts`
- **Frontend**: `web/src/app/components/ChatPane.tsx:693` - `fetchAvailableDocuments()`
