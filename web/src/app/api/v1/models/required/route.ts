import { apiFetch } from "@/lib/apiProxy";
import { logger } from "@/lib/logger";
import { withRequestLog } from "@/lib/withRequestLog";

// Model readiness for the SRE/admin dashboard (#3824): are the configured
// chat and embedding models actually in Ollama? `nyxgpt ops install` pulls
// both, so this normally reads ready -- the panel exists for the machine
// where it does not.
export const GET = withRequestLog(async function GET() {
  try {
    const res = await apiFetch(`/api/v1/models/required`, {
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
    logger.error("Failed to fetch required-model readiness:", error);
    return new Response(
      JSON.stringify({ error: "Failed to fetch required-model readiness from backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
});
