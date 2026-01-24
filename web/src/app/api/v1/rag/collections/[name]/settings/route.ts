export async function GET(
  request: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const base = process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";
  const { name } = await params;

  try {
    const res = await fetch(
      `${base}/api/v1/rag/collections/${encodeURIComponent(name)}/settings`,
      {
        method: "GET",
        headers: { "Content-Type": "application/json" },
        cache: "no-store",
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
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    console.error("Failed to fetch collection settings:", error);
    return new Response(
      JSON.stringify({ error: "Failed to fetch settings from backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
}

export async function PUT(
  request: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const base = process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";
  const { name } = await params;

  try {
    const body = await request.json();
    const res = await fetch(
      `${base}/api/v1/rag/collections/${encodeURIComponent(name)}/settings`,
      {
        method: "PUT",
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
    console.error("Failed to update collection settings:", error);
    return new Response(
      JSON.stringify({ error: "Failed to update settings" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
}
