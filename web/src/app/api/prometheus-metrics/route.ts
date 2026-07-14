export async function GET() {
  const base = process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";

  try {
    const r = await fetch(`${base}/metrics`, {
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
