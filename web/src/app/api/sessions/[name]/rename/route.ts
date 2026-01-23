export async function POST(
  request: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const base = process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";
  const { name } = await params;
  const sessionName = decodeURIComponent(name);

  // Get request body
  const body = await request.json();

  const res = await fetch(`${base}/api/v1/sessions/${encodeURIComponent(sessionName)}/rename`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });

  return new Response(res.body, {
    status: res.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
