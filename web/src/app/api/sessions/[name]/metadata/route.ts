export async function GET(
  request: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const base = process.env.MYGPT_API_BASE_URL ?? "http://127.0.0.1:8000";
  const { name } = await params;
  const sessionName = decodeURIComponent(name);

  const res = await fetch(`${base}/api/v1/sessions/${encodeURIComponent(sessionName)}/metadata`, {
    cache: "no-store",
  });

  return new Response(res.body, {
    status: res.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
