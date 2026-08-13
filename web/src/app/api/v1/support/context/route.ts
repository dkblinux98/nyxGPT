// GET-only proxy for the Support menu's environment + issue-form link (#3745).
//
// There is deliberately no POST counterpart anywhere under /support: nyxGPT
// never files an issue on the user's behalf. "File an Issue" opens GitHub's
// issue form with the version and platform this endpoint reports prefilled,
// and the user submits it themselves under their own account.
import { apiFetch } from "@/lib/apiProxy";
import { logger } from "@/lib/logger";
import { withRequestLog } from "@/lib/withRequestLog";

export const GET = withRequestLog(async function GET() {
  try {
    const res = await apiFetch("/api/v1/support/context", {
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
    logger.error("Failed to fetch the support context:", error);
    return new Response(
      JSON.stringify({ error: "Failed to fetch the support context from backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
});
