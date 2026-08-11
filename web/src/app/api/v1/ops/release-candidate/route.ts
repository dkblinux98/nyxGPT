// GET-only proxy for the PyPI publish plan (#3727).
//
// There is deliberately no POST: publishing to PyPI runs in the
// schedule/dispatch-only workflow and in `nyxgpt release publish --publish`
// -- not behind a button a browser session could press. The page renders
// the pinned install commands the backend returns, in the same
// status-plus-CLI-pointers shape as the portability surface (#3514).
import { apiFetch } from "@/lib/apiProxy";
import { logger } from "@/lib/logger";
import { withRequestLog } from "@/lib/withRequestLog";

// The backend accepts `?branch=` and `?channel=` (dev/rc/stable); forward
// them so this proxy exposes the same surface the documented API does.
const FORWARDED_PARAMS = ["branch", "channel"] as const;

export const GET = withRequestLog(async function GET(request: Request) {
  try {
    const incoming = new URL(request.url).searchParams;
    const forwarded = new URLSearchParams();
    for (const name of FORWARDED_PARAMS) {
      const value = incoming.get(name);
      if (value) forwarded.set(name, value);
    }
    const query = forwarded.toString();

    const res = await apiFetch(`/api/v1/ops/release-candidate${query ? `?${query}` : ""}`, {
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
    logger.error("Failed to fetch the PyPI publish plan:", error);
    return new Response(
      JSON.stringify({ error: "Failed to fetch the release-candidate plan from backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
});
