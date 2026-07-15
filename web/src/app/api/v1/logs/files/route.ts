import { apiFetch } from "@/lib/apiProxy";

export async function GET() {
  const r = await apiFetch(`/api/v1/logs/files`, {
    cache: "no-store",
  });

  return new Response(r.body, {
    status: r.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
