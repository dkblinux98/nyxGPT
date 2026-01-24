export async function POST(
  request: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const base = process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";
  const { name } = await params;

  try {
    const body = await request.json();
    const res = await fetch(
      `${base}/api/v1/rag/collections/${encodeURIComponent(name)}/reindex`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }
    );

    const data = await res.json();

    if (!res.ok) {
      return new Response(JSON.stringify(data), {
        status: res.status,
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    console.error("Failed to re-index collection:", error);
    return new Response(
      JSON.stringify({ error: "Failed to re-index collection" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
}
