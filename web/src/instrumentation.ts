// Next.js server instrumentation hook (Node.js runtime only -- called once
// per server process). Wires the web tier into the same OTel correlation
// backbone the API already has (nyxgpt.tracing): @vercel/otel auto-
// instruments outgoing `fetch()` calls (i.e. every apiProxy.ts `apiFetch`),
// injecting a W3C `traceparent` header with no hand-rolled forwarding, and
// exports spans to the same local otel-collector the API's
// `[tracing] otlp_endpoint` points at (#3430).
//
// Local-only, opt-in-by-default, and must degrade gracefully: a fresh
// install (or `--skip-observability`, or the collector restarting) must not
// break chat/API behavior. `registerOTel`'s exporter fails silently per the
// OTel SDK's own contract (span export failures are reported through the
// diag logger, which is a no-op unless explicitly configured -- so nothing
// here spams stderr per-request); this file only ever logs once, at boot.
import { OTLPHttpJsonTraceExporter, registerOTel } from "@vercel/otel";

const OTLP_ENDPOINT = process.env.NYXGPT_OTLP_ENDPOINT ?? "http://localhost:4318/v1/traces";
const TRACING_ENABLED = process.env.NYXGPT_TRACING_ENABLED !== "false";

export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME !== "nodejs") {
    return;
  }

  if (TRACING_ENABLED) {
    try {
      registerOTel({
        serviceName: "nyxgpt-web",
        traceExporter: new OTLPHttpJsonTraceExporter({ url: OTLP_ENDPOINT }),
      });
      // eslint-disable-next-line no-console
      console.log(
        `${new Date().toISOString()} INFO [-] nyxgpt.web: distributed tracing enabled ` +
          `(otlp_endpoint=${OTLP_ENDPOINT})`
      );
    } catch (error) {
      // Never let a tracing setup failure (e.g. a malformed endpoint URL)
      // take down the web server -- same no-op-on-failure contract as
      // nyxgpt.tracing.init_tracing.
      // eslint-disable-next-line no-console
      console.error(
        `${new Date().toISOString()} WARN [-] nyxgpt.web: failed to initialize distributed tracing: ${error}`
      );
    }
  }

  const dsn = process.env.NYXGPT_ERROR_TRACKING_DSN;
  if (dsn) {
    const Sentry = await import("@sentry/nextjs");
    Sentry.init({
      dsn,
      environment: process.env.NYXGPT_ERROR_TRACKING_ENVIRONMENT ?? "development",
      // Error capture only -- tracing is @vercel/otel's job (above), not
      // Sentry's, so its own tracing integration stays off to avoid two
      // libraries fighting over the global TracerProvider.
      tracesSampleRate: 0,
    });
  }
}

// Called by Next.js for uncaught errors from route handlers, Server
// Components, and middleware (Node.js/edge runtimes) -- reports them to the
// self-hosted, Sentry-compatible tracker (GlitchTip) alongside the browser
// errors instrumentation-client.ts reports, covering both halves of "browser
// components and API route handlers" (#3430). No-op when no DSN is
// configured (the default), same as the Python side's error_tracking.py.
export async function onRequestError(
  ...args: Parameters<NonNullable<typeof import("@sentry/nextjs").captureRequestError>>
): Promise<void> {
  if (!process.env.NYXGPT_ERROR_TRACKING_DSN) {
    return;
  }
  const Sentry = await import("@sentry/nextjs");
  Sentry.captureRequestError(...args);
}
