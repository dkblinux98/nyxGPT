export async function GET() {
  const base = process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";

  try {
    const res = await fetch(`${base}/api/v1/rag/collections`, {
      method: "GET",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });

    if (!res.ok) {
      return new Response(
        JSON.stringify({ error: `Backend returned ${res.status}` }),
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
    console.error("Failed to fetch RAG collections:", error);
    return new Response(
      JSON.stringify({ error: "Failed to fetch collections from backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
}

export async function POST(request: Request) {
  const base = process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";

  try {
    const body = await request.json();
    const res = await fetch(`${base}/api/v1/rag/collections`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

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
    console.error("Failed to create collection:", error);
    return new Response(
      JSON.stringify({ error: "Failed to create collection" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
}
