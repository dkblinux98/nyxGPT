import { apiFetch } from "@/lib/apiProxy";

export async function POST(request: Request) {
  try {
    const body = await request.json().catch(() => ({}));
    const res = await apiFetch(`/api/v1/self-heal/toggle`, {
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
    console.error("Failed to toggle self-heal:", error);
    return new Response(
      JSON.stringify({ error: "Failed to toggle self-heal" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
}
