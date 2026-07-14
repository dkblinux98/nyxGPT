export async function GET() {
  const base =
    process.env.NYXGPT_API_BASE_URL ??
    "http://127.0.0.1:8000";

  try {
    const r = await fetch(`${base}/api/v1/monitoring`, {
      cache: "no-store",
    });

    return new Response(r.body, {
      status: r.status,
      headers: {
        "Content-Type": "application/json",
      },
    });
  } catch {
    return new Response(JSON.stringify({ error: "monitoring backend unavailable" }), {
      status: 502,
      headers: {
        "Content-Type": "application/json",
      },
    });
  }
}
