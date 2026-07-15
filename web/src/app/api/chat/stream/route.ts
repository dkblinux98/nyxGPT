import { NextRequest } from "next/server";
import { Agent } from "undici";
import { apiFetch } from "@/lib/apiProxy";

// Undici defaults can abort long-lived streaming responses.
// Configure a local agent with no body timeout.
const agent = new Agent({
  bodyTimeout: 0, // 0 disables the body timeout entirely
  headersTimeout: 60_000,
});

function ts(): string {
  // ISO timestamp for log readability
  return new Date().toISOString();
}


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
  const reqId = Math.random().toString(16).slice(2, 10);

  let body: ChatStreamRequest;
  try {
    body = await req.json();
  } catch (e) {
    console.error(`[${ts()}] [web] [chat/stream] [${reqId}] invalid json:`, e);
    return new Response(JSON.stringify({ error: "Invalid JSON" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const upstreamPath = "/api/v1/chat/stream";

  // Log minimal metadata (don’t dump full prompts)
  const session = typeof body?.session === "string" ? body.session : undefined;
  const promptLen = typeof body?.prompt === "string" ? body.prompt.length : undefined;
  console.log(
    `[${ts()}] [web] [chat/stream] [${reqId}] start session=${session ?? ""} prompt_len=${
      promptLen ?? ""
    } upstream=${upstreamPath}`
  );

  let upstream: Response;
  try {
    upstream = await apiFetch(upstreamPath, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // Client capability hints for content negotiation
        "Accept": "text/event-stream",
        "X-Client-Supports-SSE": "true",
        "X-Client-Supports-Structured-Events": "true",
        "X-Client-Supports-Streaming": "true",
        "X-Client-Version": "web-ui/1.0.0",
        "X-Client-Max-Event-Size": "0",  // 0 = unlimited
      },
      body: JSON.stringify(body),
      // @ts-expect-error - Next/undici fetch supports dispatcher
      dispatcher: agent,
      cache: "no-store",
    });
  } catch (e) {
    console.error(
      `[${ts()}] [web] [chat/stream] [${reqId}] upstream fetch failed after ${nowMs() - startMs}ms:`,
      e
    );
    return new Response(JSON.stringify({ error: "Upstream unreachable" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }

  console.log(
    `[${ts()}] [web] [chat/stream] [${reqId}] upstream status=${upstream.status} after ${nowMs() - startMs}ms`
  );

  if (!upstream.ok || !upstream.body) {
    let detail = "";
    try {
      detail = await upstream.text();
    } catch {
      // ignore
    }

    console.error(
      `[${ts()}] [web] [chat/stream] [${reqId}] upstream not ok after ${nowMs() - startMs}ms detail_len=${detail.length}`
    );

    return new Response(
      JSON.stringify({ error: "Upstream chat stream failed", detail }),
      { status: 502, headers: { "Content-Type": "application/json" } }
    );
  }

  // Wrap the upstream stream so we can log “time to first byte” and end-of-stream.
  const reader = upstream.body.getReader();
  const encoder = new TextEncoder();

  let sawFirst = false;
  let totalBytes = 0;

  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const { done, value } = await reader.read();
        if (done) {
          console.log(
            `[${ts()}] [web] [chat/stream] [${reqId}] end after ${nowMs() - startMs}ms bytes=${totalBytes}`
          );
          controller.close();
          return;
        }

        if (value) {
          totalBytes += value.byteLength;
          if (!sawFirst) {
            sawFirst = true;
            console.log(
              `[${ts()}] [web] [chat/stream] [${reqId}] first_byte after ${nowMs() - startMs}ms`
            );
          }
          controller.enqueue(value);
        }
      } catch (e) {
        console.error(
          `[${ts()}] [web] [chat/stream] [${reqId}] stream error after ${nowMs() - startMs}ms:`,
          e
        );
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
        await reader.cancel();
      } catch {
        // ignore
      }
      console.log(
        `[${ts()}] [web] [chat/stream] [${reqId}] client_cancel after ${nowMs() - startMs}ms bytes=${totalBytes}`
      );
    },
  });

  return new Response(stream, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform, no-store",
      "Connection": "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}