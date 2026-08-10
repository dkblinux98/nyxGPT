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

export const POST = withRequestLog(async function POST(request: Request) {
  try {
    const body = await request.json().catch(() => ({}));
    const res = await apiFetch(`/api/v1/cloud/deploy`, {
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
    logger.error("Failed to deploy the cloud stack:", error);
    return new Response(JSON.stringify({ error: "Failed to deploy the cloud stack" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
});
