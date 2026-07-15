import { apiFetch } from "@/lib/apiProxy";

export async function GET() {
  try {
    const r = await apiFetch(`/api/v1/log-aggregation`, {
      cache: "no-store",
    });

    return new Response(r.body, {
      status: r.status,
      headers: {
        "Content-Type": "application/json",
      },
    });
  } catch {
    return new Response(JSON.stringify({ error: "log aggregation backend unavailable" }), {
      status: 502,
      headers: {
        "Content-Type": "application/json",
      },
    });
  }
}
