export async function POST(request: Request) {
  const base = process.env.MYGPT_API_BASE_URL ?? "http://127.0.0.1:8000";

  const body = await request.json();

  const res = await fetch(`${base}/api/v1/sessions/init`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  return new Response(res.body, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
