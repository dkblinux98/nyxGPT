const BASE_URL = process.env.NYXGPT_API_BASE_URL ?? 'http://127.0.0.1:8000';

export async function GET() {
  try {
    const r = await fetch(`${BASE_URL}/api/v1/admin/overview`, {
      cache: 'no-store',
    });

    return new Response(r.body, {
      status: r.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch {
    return new Response(JSON.stringify({ error: 'admin overview backend unavailable' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}
