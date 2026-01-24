export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const base = process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";
  const { name } = await params;

  try {
    const res = await fetch(`${base}/api/v1/rag/collections/${encodeURIComponent(name)}`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
    });

    if (!res.ok) {
      const errorData = await res.json().catch(() => ({ error: `Backend returned ${res.status}` }));
      return new Response(
        JSON.stringify(errorData),
        {
          status: res.status,
          headers: { "Content-Type": "application/json" },
        }
      );
    }

    const data = await res.json();
    return new Response(JSON.stringify(data), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    console.error(`Failed to delete collection '${name}':`, error);
    return new Response(
      JSON.stringify({ error: "Failed to delete collection from backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
}
