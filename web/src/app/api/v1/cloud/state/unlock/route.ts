import { apiFetch } from "@/lib/apiProxy";
import { logger } from "@/lib/logger";
import { withRequestLog } from "@/lib/withRequestLog";

export const POST = withRequestLog(async function POST(request: Request) {
  try {
    const body = await request.json().catch(() => ({}));
    const res = await apiFetch(`/api/v1/cloud/state/unlock`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });

    const data = await res.json();

    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    logger.error("Failed to release the Terraform state lock:", error);
    return new Response(JSON.stringify({ error: "Failed to release the Terraform state lock" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
});
