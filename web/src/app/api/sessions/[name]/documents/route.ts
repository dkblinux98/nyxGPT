const BASE = process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;
  const sessionName = decodeURIComponent(name);

  const res = await fetch(
    `${BASE}/api/v1/sessions/${encodeURIComponent(sessionName)}/documents`
  );

  return new Response(res.body, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}

export async function POST(
  request: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;
  const sessionName = decodeURIComponent(name);
  const body = await request.json();

  const res = await fetch(
    `${BASE}/api/v1/sessions/${encodeURIComponent(sessionName)}/documents`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }
  );

  return new Response(res.body, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
