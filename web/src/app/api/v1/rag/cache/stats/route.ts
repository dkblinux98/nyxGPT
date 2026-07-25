import { apiFetch } from "@/lib/apiProxy";

export async function GET() {
  try {
    const res = await apiFetch(`/api/v1/rag/cache/stats`, {
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
    console.error("Failed to fetch query cache stats:", error);
    return new Response(
      JSON.stringify({ error: "Failed to fetch query cache stats from backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
}
