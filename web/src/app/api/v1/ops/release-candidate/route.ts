// GET-only proxy for the PyPI publish plan (#3727).
//
// There is deliberately no POST: publishing to PyPI runs in the
// dispatch-only workflow and in `nyxgpt release publish --publish`
// -- not behind a button a browser session could press. What it returns is
// the plan plus the pinned install commands the backend already documents,
// so a consumer points at the CLI rather than keeping its own copy.
//
// No in-app page consumes this today: its only reader was the Portability
// and Acceptance screen, removed in #3803. The proxy is kept because the
// plan is still read over HTTP (`GET /api/v1/ops/release-candidate`,
// docs/api.md) -- removing it was not in that issue's scope. If nothing
// grows a UI for it, retiring this route and its test is the tidy-up.
import { apiFetch } from "@/lib/apiProxy";
import { logger } from "@/lib/logger";
import { withRequestLog } from "@/lib/withRequestLog";

// The backend accepts `?branch=` and `?channel=` (rc/stable); forward
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
