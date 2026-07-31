// Request-scoped correlation id, ambient across a route handler's execution
// via Node's AsyncLocalStorage -- lets `logger.ts` and `apiProxy.ts` read the
// current request's id without either the route handler or the call site
// having to thread it through explicitly (#3430).
import { AsyncLocalStorage } from "node:async_hooks";
import { context as otelContext, trace } from "@opentelemetry/api";

interface RequestStore {
  requestId: string;
}

const requestContextStorage = new AsyncLocalStorage<RequestStore>();

export function getRequestId(): string | undefined {
  return requestContextStorage.getStore()?.requestId;
}

export function withRequestId<T>(requestId: string, fn: () => T): T {
  return requestContextStorage.run({ requestId }, fn);
}

// Reuses the client-supplied X-Request-Id; failing that, derives one from
// the active OTel trace (started by @vercel/otel's incoming-request
// instrumentation) so the id matches the same trace Jaeger has; failing
// that, generates a fresh one -- mirrors nyxgpt.app's
// add_request_id_and_limits middleware precedence exactly. `request` is
// optional only so a handler invoked without one (Next.js itself always
// passes it, but some route handlers historically declared no parameter,
// and existing unit tests call the exported handler directly with no
// arguments) still gets a usable id instead of throwing.
export function resolveRequestId(request?: Request): string {
  const header = request?.headers.get("x-request-id");
  if (header) {
    return header;
  }
  const spanContext = trace.getSpan(otelContext.active())?.spanContext();
  if (spanContext && trace.isSpanContextValid(spanContext)) {
    return spanContext.traceId;
  }
  return crypto.randomUUID();
}
