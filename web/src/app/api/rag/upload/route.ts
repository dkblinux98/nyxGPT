export async function POST(request: Request) {
  const base = process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";
  const formData = await request.formData();

  const res = await fetch(`${base}/api/v1/rag/upload`, {
    method: "POST",
    body: formData,
  });

  return new Response(res.body, {
    status: res.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
