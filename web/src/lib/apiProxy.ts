// Canonical server-side helper for Next.js API proxy routes to reach the
// nyxGPT backend. All routes under web/src/app/api/**/route.ts MUST use this
// instead of calling `fetch` directly against a hand-rolled base URL, so the
// base URL resolution and auth header attachment stay in exactly one place.
import { getRequestId } from "./requestContext";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";

function apiBaseUrl(): string {
  const base = process.env.NYXGPT_API_BASE_URL ?? DEFAULT_API_BASE_URL;
  return base.replace(/\/+$/, "");
}

export function apiUrl(path: string): string {
  return `${apiBaseUrl()}${path.startsWith("/") ? path : `/${path}`}`;
}

// Attaches the API key the backend expects (NYXGPT_AUTH_API_KEY / X-API-Key)
// to `headers` when one is configured, without clobbering a caller-supplied
// value. Shared by apiFetch() and any route that talks to the backend
// through a transport other than `fetch` (see chat/stream/route.ts).
export function attachApiKey(headers: Headers): Headers {
  const apiKey = process.env.NYXGPT_AUTH_API_KEY;
  if (apiKey && !headers.has("X-API-Key")) {
    headers.set("X-API-Key", apiKey);
  }
  return headers;
}

// Thin wrapper around `fetch` that resolves `path` against the configured
// backend base URL (NYXGPT_API_BASE_URL), attaches the API key the backend
// expects (NYXGPT_AUTH_API_KEY / X-API-Key) when one is configured, and
// forwards the current request's correlation id as `X-Request-Id` (#3430) --
// picked up ambiently via requestContext.ts, so call sites never need to
// pass it explicitly. The W3C `traceparent` header itself is injected by
// @vercel/otel's automatic fetch instrumentation (see instrumentation.ts),
// not by this helper -- that's the "no hand-rolled header forwarding" half
// of the correlation backbone. Non-fetch transports (chat/stream's undici
// request()) must attach both headers themselves via attachApiKey + an
// explicit X-Request-Id set.
export function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = attachApiKey(new Headers(init.headers));
  const requestId = getRequestId();
  if (requestId && !headers.has("X-Request-Id")) {
    headers.set("X-Request-Id", requestId);
  }
  return fetch(apiUrl(path), { ...init, headers });
}
