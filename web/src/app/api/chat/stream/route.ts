import { NextRequest } from "next/server";
import { request as undiciRequest } from "undici";
import { apiUrl, attachApiKey } from "@/lib/apiProxy";
import { logger } from "@/lib/logger";
import { resolveRequestId, withRequestId } from "@/lib/requestContext";

// This route drives the upstream call through the `undici` package's own
// request() rather than Next's built-in `fetch`. Next 16 bundles its own
// (newer-major) undici for global fetch; handing that fetch a dispatcher
// built from the `undici` version pinned in package.json broke instantly
// with UND_ERR_INVALID_ARG (the v7-bundled fetch's dispatch-handler
// interface renamed the v6 Agent's `onError` callback) — see #3440. Calling
// this package's own request() sidesteps the coupling entirely: the HTTP
// client and every option it understands come from the exact same undici
// major version, regardless of what Next/Node happen to bundle for global
// fetch. Do not resurrect a `dispatcher`/Agent passed into `fetch()` here.
const HEADERS_TIMEOUT_MS =
  Number(process.env.NYXGPT_CHAT_STREAM_HEADERS_TIMEOUT_MS) || 300_000; // 5 min: must outlast a cold local-model load, not just a warm one

// High-resolution timer compatible with browsers and Node.js
function nowMs(): number {
  if (typeof performance !== "undefined" && typeof performance.now === "function") {
    return performance.now();
  }
  return Date.now();
}

interface ChatStreamRequest {
  session?: string;
  prompt?: string;
  model?: string;
  system?: string;
  rag_enabled?: boolean;
  [key: string]: unknown;
}

export async function POST(req: NextRequest) {
  const startMs = nowMs();
  // Resolved once and threaded explicitly (rather than relying on
  // withRequestLog's ambient AsyncLocalStorage context) because this
  // handler's ReadableStream `pull`/`cancel` callbacks fire long after the
  // handler itself has returned -- logging from them needs the same request
  // id this handler attached as X-Request-Id on the upstream call.
  const requestId = resolveRequestId(req);
  const log = {
    info: (message: string) => withRequestId(requestId, () => logger.info(message)),
    error: (message: string, error?: unknown) =>
      withRequestId(requestId, () => logger.error(message, error)),
  };

  let body: ChatStreamRequest;
  try {
    body = await req.json();
  } catch (e) {
    log.error("chat/stream: invalid json", e);
    return new Response(JSON.stringify({ error: "Invalid JSON" }), {
      status: 400,
      headers: { "Content-Type": "application/json", "X-Request-Id": requestId },
    });
  }

  const upstreamPath = "/api/v1/chat/stream";

  // Log minimal metadata (don’t dump full prompts)
  const session = typeof body?.session === "string" ? body.session : undefined;
  const promptLen = typeof body?.prompt === "string" ? body.prompt.length : undefined;
  log.info(
    `chat/stream: start session=${session ?? ""} prompt_len=${promptLen ?? ""} upstream=${upstreamPath}`
  );

  let statusCode: number;
  let upstreamBody: Awaited<ReturnType<typeof undiciRequest>>["body"];
  try {
    const headers = attachApiKey(
      new Headers({
        "Content-Type": "application/json",
        // Client capability hints for content negotiation
        "Accept": "text/event-stream",
        "X-Client-Supports-SSE": "true",
        "X-Client-Supports-Structured-Events": "true",
        "X-Client-Supports-Streaming": "true",
        "X-Client-Version": "web-ui/1.0.0",
        "X-Client-Max-Event-Size": "0", // 0 = unlimited
      })
    );
    // This route bypasses apiFetch (fetch-based), so forward the correlation
    // id explicitly -- same header apiFetch attaches ambiently (#3430).
    if (!headers.has("X-Request-Id")) {
      headers.set("X-Request-Id", requestId);
    }

    const result = await undiciRequest(apiUrl(upstreamPath), {
      method: "POST",
      headers: Object.fromEntries(headers.entries()),
      body: JSON.stringify(body),
      headersTimeout: HEADERS_TIMEOUT_MS,
      bodyTimeout: 0, // a long-lived SSE stream must never time out on an idle gap between chunks
    });
    statusCode = result.statusCode;
    upstreamBody = result.body;
  } catch (e) {
    log.error(`chat/stream: upstream fetch failed after ${nowMs() - startMs}ms`, e);
    return new Response(JSON.stringify({ error: "Upstream unreachable" }), {
      status: 502,
      headers: { "Content-Type": "application/json", "X-Request-Id": requestId },
    });
  }

  log.info(`chat/stream: upstream status=${statusCode} after ${nowMs() - startMs}ms`);

  // undici's request() only resolves with the final response — informational
  // (1xx) statuses are consumed internally, so statusCode is always >= 200.
  if (statusCode >= 300) {
    let detail = "";
    try {
      detail = await upstreamBody.text();
    } catch {
      // ignore
    }

    log.error(
      `chat/stream: upstream not ok after ${nowMs() - startMs}ms detail_len=${detail.length}`
    );

    // Pass the real upstream status through instead of flattening every
    // failure to a generic 502 — the browser needs to tell "backend down"
    // apart from "backend rejected the request" / "model runtime error".
    return new Response(
      JSON.stringify({ error: "Upstream chat stream failed", detail }),
      {
        status: statusCode,
        headers: { "Content-Type": "application/json", "X-Request-Id": requestId },
      }
    );
  }

  // Wrap the upstream stream so we can log “time to first byte” and end-of-stream.
  const iterator = upstreamBody[Symbol.asyncIterator]();
  const encoder = new TextEncoder();

  let sawFirst = false;
  let totalBytes = 0;

  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const { done, value } = await iterator.next();
        if (done) {
          log.info(`chat/stream: end after ${nowMs() - startMs}ms bytes=${totalBytes}`);
          controller.close();
          return;
        }

        if (value) {
          totalBytes += value.byteLength;
          if (!sawFirst) {
            sawFirst = true;
            log.info(`chat/stream: first_byte after ${nowMs() - startMs}ms`);
          }
          controller.enqueue(value);
        }
      } catch (e) {
        log.error(`chat/stream: stream error after ${nowMs() - startMs}ms`, e);
        // best effort: emit a newline + error marker to client
        try {
          controller.enqueue(encoder.encode("\n[stream error]\n"));
        } catch {
          // ignore
        }
        controller.error(e);
      }
    },
    async cancel() {
      try {
        upstreamBody.destroy();
      } catch {
        // ignore
      }
      log.info(`chat/stream: client_cancel after ${nowMs() - startMs}ms bytes=${totalBytes}`);
    },
  });

  return new Response(stream, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform, no-store",
      "Connection": "keep-alive",
      "X-Accel-Buffering": "no",
      "X-Request-Id": requestId,
    },
  });
}
