// GET-only proxy for what the Support menu needs before it offers anything
// (#3745, #3811): this install's version and platform, the prefilled GitHub
// form, and `can_submit` -- whether nyxGPT holds a credential and can file
// the ticket itself.
//
// Reading that changes nothing, so this route stays GET-only. Filing is a
// separate route (`/api/v1/support/tickets`) and is the only write on the
// Support surface.
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
