// GET-only proxy for the packaged documentation index (#3745).
//
// The docs a user reads come out of the installed package, not a checkout --
// an install from PyPI or Homebrew has no repo to read from. This route just
// forwards the index; the per-document render lives in `[slug]/route.ts`.
import { apiFetch } from "@/lib/apiProxy";
import { logger } from "@/lib/logger";
import { withRequestLog } from "@/lib/withRequestLog";

export const GET = withRequestLog(async function GET() {
  try {
    const res = await apiFetch("/api/v1/support/docs", {
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
    logger.error("Failed to fetch the documentation index:", error);
    return new Response(
      JSON.stringify({ error: "Failed to fetch the documentation index from backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
});
