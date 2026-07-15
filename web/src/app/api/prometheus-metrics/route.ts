import { apiFetch } from "@/lib/apiProxy";

export async function GET() {
  try {
    const r = await apiFetch(`/metrics`, {
      cache: "no-store",
    });

    return new Response(r.body, {
      status: r.status,
      headers: {
        "Content-Type": r.headers.get("content-type") ?? "text/plain; charset=utf-8",
      },
    });
  } catch {
    return new Response("# metrics backend unavailable\n", {
      status: 502,
      headers: {
        "Content-Type": "text/plain; charset=utf-8",
      },
    });
  }
}
