export async function POST(request: Request) {
  const base = process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";

  try {
    const body = await request.json().catch(() => ({}));
    const res = await fetch(`${base}/api/v1/deploy/switch`, {
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
    console.error("Failed to switch deployment color:", error);
    return new Response(
      JSON.stringify({ error: "Failed to switch deployment color" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
}
