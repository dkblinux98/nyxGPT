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
    // instance health, and only asks AWS about the recorded Dedicated Host,
    // when asked -- so a polling caller stays free of network calls (see
    // cloud_deploy.deploy_status).
    //
    // Every opt-in the backend accepts must be forwarded here, and each needs
    // a test asserting the backend URL carries it. #4136 shipped
    // `verify_host=true` on the page's explicit load while this handler still
    // forwarded `probe_health` alone, so the dashboard asked for the AWS
    // confirmation and the proxy quietly dropped the request: the page read
    // "never confirmed at AWS" no matter how many times it was reloaded.
    const incoming = new URL(request.url).searchParams;
    const forwarded = new URLSearchParams();
    for (const name of ["probe_health", "verify_host"]) {
      const value = incoming.get(name);
      if (value) {
        forwarded.set(name, value);
      }
    }
    const query = forwarded.toString();
    const res = await apiFetch(
      `/api/v1/cloud/deploy${query ? `?${query}` : ""}`,
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
