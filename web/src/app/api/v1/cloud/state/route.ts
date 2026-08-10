import { apiFetch } from "@/lib/apiProxy";
import { logger } from "@/lib/logger";
import { withRequestLog } from "@/lib/withRequestLog";

export const GET = withRequestLog(async function GET(request: Request) {
  try {
    // `?verify=true` turns the cheap, offline status read into one that also
    // confirms the bucket/lock table against AWS -- forwarded rather than
    // hardcoded so the dashboard's poll stays free.
    const verify = new URL(request.url).searchParams.get("verify") === "true";
    const res = await apiFetch(`/api/v1/cloud/state${verify ? "?verify=true" : ""}`, {
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
    logger.error("Failed to fetch Terraform state status:", error);
    return new Response(
      JSON.stringify({ error: "Failed to fetch Terraform state status from backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
});
