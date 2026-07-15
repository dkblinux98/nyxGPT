import { apiFetch } from "@/lib/apiProxy";

export async function POST() {
  try {
    const res = await apiFetch(`/api/v1/canary/rollback`, {
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
    console.error("Failed to roll back canary rollout:", error);
    return new Response(
      JSON.stringify({ error: "Failed to roll back canary rollout" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
}
