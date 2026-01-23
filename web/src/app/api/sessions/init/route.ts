export async function POST(request: Request) {
  const base = process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";

  let body;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: { message: "Invalid JSON in request body" } }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  try {
    const res = await fetch(`${base}/api/v1/sessions/init`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    const responseText = await res.text();

    return new Response(responseText, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    console.error("Failed to proxy session init request:", error);
    return new Response(JSON.stringify({ error: { message: "Backend service unavailable" } }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }
}
