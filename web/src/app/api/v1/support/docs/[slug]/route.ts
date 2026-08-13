// GET-only proxy for one rendered documentation page (#3745).
//
// The slug is passed straight through: the backend (`nyxgpt.support`) is
// what decides whether it names a packaged document, and it rejects anything
// that could reach outside the packaged docs directory. A slug that names
// nothing comes back as a 404, which this forwards rather than flattening
// into a 502.
import { apiFetch } from "@/lib/apiProxy";
import { logger } from "@/lib/logger";
import { withRequestLog } from "@/lib/withRequestLog";

export const GET = withRequestLog(async function GET(
  _request: Request,
  { params }: { params: Promise<{ slug: string }> }
) {
  const { slug } = await params;

  try {
    const res = await apiFetch(`/api/v1/support/docs/${encodeURIComponent(slug)}`, {
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
    logger.error(`Failed to fetch document '${slug}':`, error);
    return new Response(
      JSON.stringify({ error: "Failed to fetch the document from backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
});
