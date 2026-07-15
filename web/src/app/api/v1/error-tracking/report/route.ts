import { apiFetch } from "@/lib/apiProxy";

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const res = await apiFetch(`/api/v1/error-tracking/report`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    const data = await res.json();
    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    // Best-effort: client error reporting must never surface its own errors.
    return new Response(JSON.stringify({ status: "unavailable" }), {
      status: 202,
      headers: { "Content-Type": "application/json" },
    });
  }
}
