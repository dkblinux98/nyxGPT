// GET-only proxy for the repo-less portability matrix (P6-16, #3516).
//
// There is deliberately no POST here, for a stronger reason than the #3514
// status-plus-CLI-pointers decision that shapes the cloud surface: the matrix
// describes the *product* -- which artifacts are published, which commands
// are wrapped, which targets still need a checkout -- not the state of this
// machine, so there is nothing here to act on from a browser in the first
// place. The wrapped CLI commands come back in the payload's `commands` so
// the page renders pointers rather than buttons.
import { apiFetch } from "@/lib/apiProxy";
import { logger } from "@/lib/logger";
import { withRequestLog } from "@/lib/withRequestLog";

export const GET = withRequestLog(async function GET() {
  try {
    const res = await apiFetch(`/api/v1/ops/portability`, {
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
    logger.error("Failed to fetch the portability matrix:", error);
    return new Response(
      JSON.stringify({ error: "Failed to fetch the portability matrix from backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
});
