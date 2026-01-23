export async function GET(
  request: Request,
  { params }: { params: Promise<{ name: string; index: string }> }
) {
  const base = process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";
  const { name, index } = await params;
  const sessionName = decodeURIComponent(name);
  const messageIndex = decodeURIComponent(index);

  const res = await fetch(
    `${base}/api/v1/sessions/${encodeURIComponent(sessionName)}/messages/${messageIndex}/rag`,
    {
      cache: "no-store",
    }
  );

  return new Response(res.body, {
    status: res.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
