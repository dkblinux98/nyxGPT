// Read-only by design. The owner's 2026-08-09 decision on #3514 makes the
// cloud surface status-plus-CLI-pointers, so this proxy exposes GET and
// nothing else: with no POST here, the browser has no path to a deploy at
// all, rather than merely no button pointing at one. The backend's
// `POST /api/v1/cloud/deploy` still exists for the CLI and other API clients.
import { apiFetch } from "@/lib/apiProxy";
import { logger } from "@/lib/logger";
import { withRequestLog } from "@/lib/withRequestLog";

export const GET = withRequestLog(async function GET(request: Request) {
  try {
    // Forwarded rather than always-on: the backend only probes the tunneled
    // instance health when asked, so a polling caller stays free of network
    // calls (see cloud_deploy.deploy_status).
    const probeHealth = new URL(request.url).searchParams.get("probe_health");
    const res = await apiFetch(
      `/api/v1/cloud/deploy${probeHealth ? `?probe_health=${encodeURIComponent(probeHealth)}` : ""}`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        cache: "no-store",
      }
    );

    const data = await res.json();

    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    logger.error("Failed to fetch cloud deployment status:", error);
    return new Response(
      JSON.stringify({ error: "Failed to fetch cloud deployment status from backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
});
