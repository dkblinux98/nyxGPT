const BASE_URL = process.env.MYGPT_API_BASE_URL ?? 'http://127.0.0.1:8000';

export async function GET() {
  const r = await fetch(`${BASE_URL}/api/v1/config`, {
    cache: 'no-store',
  });

  return new Response(r.body, {
    status: r.status,
    headers: {
      'Content-Type': 'application/json',
    },
  });
}

export async function POST(request: Request) {
  const body = await request.json();

  const r = await fetch(`${BASE_URL}/api/v1/config`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
    cache: 'no-store',
  });

  return new Response(r.body, {
    status: r.status,
    headers: {
      'Content-Type': 'application/json',
    },
  });
}
