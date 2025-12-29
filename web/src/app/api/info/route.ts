export async function GET() {
  const base =
    process.env.MYGPT_API_BASE_URL ??
    "http://127.0.0.1:8000";

  const r = await fetch(`${base}/api/v1/info`, {
    cache: "no-store",
  });

  return new Response(r.body, {
    status: r.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
