import { apiFetch } from "@/lib/apiProxy";

export async function POST(request: Request) {
  try {
    const body = await request.json().catch(() => ({}));
    const res = await apiFetch(`/api/v1/infra/kubernetes/install`, {
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
    console.error("Failed to deploy to Kubernetes:", error);
    return new Response(
      JSON.stringify({ error: "Failed to deploy to Kubernetes" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
}
