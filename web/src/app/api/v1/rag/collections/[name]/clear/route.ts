import { apiFetch } from "@/lib/apiProxy";
import { logger } from "@/lib/logger";
import { withRequestLog } from "@/lib/withRequestLog";

export const POST = withRequestLog(async function POST(
  _request: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;

  try {
    const res = await apiFetch(
      `/api/v1/rag/collections/${encodeURIComponent(name)}/clear`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      }
    );

    const data = await res.json();

    if (!res.ok) {
      return new Response(JSON.stringify(data), {
        status: res.status,
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    logger.error(`Failed to clear collection '${name}':`, error);
    return new Response(
      JSON.stringify({ error: "Failed to clear collection from backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
});
