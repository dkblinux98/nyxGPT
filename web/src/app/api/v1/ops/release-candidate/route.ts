// GET-only proxy for the release-candidate plan (#3727).
//
// There is deliberately no POST: cutting an RC publishes to PyPI with the
// owner's repo and PyPI credentials, so it lives in the dispatch-only
// workflow and in `nyxgpt release rc --publish` -- not behind a button a
// browser session could press. The page renders the pinned install commands
// the backend returns, in the same status-plus-CLI-pointers shape as the
// portability surface (#3514).
import { apiFetch } from "@/lib/apiProxy";
import { logger } from "@/lib/logger";
import { withRequestLog } from "@/lib/withRequestLog";

export const GET = withRequestLog(async function GET() {
  try {
    const res = await apiFetch("/api/v1/ops/release-candidate", {
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
    logger.error("Failed to fetch the release-candidate plan:", error);
    return new Response(
      JSON.stringify({ error: "Failed to fetch the release-candidate plan from backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
});
