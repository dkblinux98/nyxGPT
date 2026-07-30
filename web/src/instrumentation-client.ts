// Next.js client instrumentation hook -- loaded in the browser before
// hydration. Completes the "browser click" end of the correlation backbone
// (#3430): a WebTracerProvider + FetchInstrumentation means every
// `fetch()` the browser makes to a Next.js API route (chat, sessions, RAG,
// admin) starts (or continues) a span and gets a W3C `traceparent` header,
// which @vercel/otel's server-side fetch instrumentation (instrumentation.ts)
// then continues into the call to FastAPI, and nyxgpt.tracing's urllib
// instrumentation continues again into Ollama -- one trace, browser to
// Ollama and back.
//
// Local-only and opt-in-by-default like the rest of nyxGPT's observability
// stack: points at the same otel-collector the API and Next.js server do,
// never an external endpoint. Degrades gracefully -- the OTLP exporter's
// failures are reported through @opentelemetry/api's diag logger (a no-op
// unless a handler is registered, which this file doesn't do), so a
// collector that isn't up yet produces no per-request console noise, just
// silently-dropped spans, exactly like the Python side.
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { FetchInstrumentation } from "@opentelemetry/instrumentation-fetch";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { BatchSpanProcessor, WebTracerProvider } from "@opentelemetry/sdk-trace-web";

const OTLP_ENDPOINT =
  process.env.NEXT_PUBLIC_NYXGPT_OTLP_ENDPOINT ?? "http://localhost:4318/v1/traces";
const TRACING_ENABLED = process.env.NEXT_PUBLIC_NYXGPT_TRACING_ENABLED !== "false";

if (TRACING_ENABLED && typeof window !== "undefined") {
  try {
    const provider = new WebTracerProvider({
      resource: resourceFromAttributes({ "service.name": "nyxgpt-web-browser" }),
      spanProcessors: [new BatchSpanProcessor(new OTLPTraceExporter({ url: OTLP_ENDPOINT }))],
    });
    provider.register();

    registerInstrumentations({
      tracerProvider: provider,
      instrumentations: [
        new FetchInstrumentation({
          // Only trace same-origin API calls (Next.js route handlers under
          // /api/**) -- never third-party requests the browser might make.
          propagateTraceHeaderCorsUrls: [new RegExp(`^${window.location.origin}/api/`)],
        }),
      ],
    });
  } catch (error) {
    // eslint-disable-next-line no-console
    console.error("nyxgpt-web: failed to initialize browser tracing (degrading gracefully):", error);
  }
}

// Browser/client error tracking to the local GlitchTip instance (#3430),
// covering the client-component half of "browser components and API route
// handlers" (instrumentation.ts's onRequestError covers the server half).
// No-op with no DSN configured (the default) -- mirrors error_tracking.py.
const dsn = process.env.NEXT_PUBLIC_NYXGPT_ERROR_TRACKING_DSN;
if (dsn) {
  void import("@sentry/nextjs").then((Sentry) => {
    Sentry.init({
      dsn,
      environment: process.env.NEXT_PUBLIC_NYXGPT_ERROR_TRACKING_ENVIRONMENT ?? "development",
      // Error capture only -- tracing above is handled by the plain OTel Web
      // SDK, not Sentry's own tracing integration.
      tracesSampleRate: 0,
    });
  });
}
