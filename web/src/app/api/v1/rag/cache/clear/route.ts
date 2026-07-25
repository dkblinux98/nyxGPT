import { apiFetch } from "@/lib/apiProxy";

export async function POST() {
  try {
    const res = await apiFetch(`/api/v1/rag/cache/clear`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });

    const data = await res.json();

    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    console.error("Failed to clear query cache:", error);
    return new Response(
      JSON.stringify({ error: "Failed to clear query cache" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
}
