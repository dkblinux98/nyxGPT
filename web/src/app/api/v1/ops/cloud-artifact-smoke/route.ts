// Proxy for the containerized cloud artifact-install smoke (#3784).
//
// GET reads the last run (cheap, pollable). POST starts one in the background
// and returns immediately -- a run builds an image, boots systemd, provisions
// Node/Docker/Ollama and waits for services, which is tens of minutes and far
// past any HTTP timeout, so the panel polls GET for the verdict.
//
// Unlike the AWS cloud surface (#3514: status plus CLI pointers), this one
// gets a real button: the run costs nothing, touches no AWS account and
// creates nothing outside a disposable local container.
import { apiFetch } from "@/lib/apiProxy";
import { logger } from "@/lib/logger";
import { withRequestLog } from "@/lib/withRequestLog";

export const GET = withRequestLog(async function GET() {
  try {
    const res = await apiFetch("/api/v1/ops/cloud-artifact-smoke", {
      method: "GET",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });

    const data = await res.json();

    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    logger.error("Failed to fetch the cloud artifact smoke status:", error);
    return new Response(
      JSON.stringify({ error: "Failed to fetch the cloud artifact smoke status from backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
});

export const POST = withRequestLog(async function POST(request: Request) {
  try {
    const body = await request.json().catch(() => ({}));
    const res = await apiFetch("/api/v1/ops/cloud-artifact-smoke", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
      cache: "no-store",
    });

    const data = await res.json();

    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    logger.error("Failed to start the cloud artifact smoke:", error);
    return new Response(JSON.stringify({ error: "Failed to start the cloud artifact smoke" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
});
