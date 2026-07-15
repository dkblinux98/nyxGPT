import { apiFetch } from '@/lib/apiProxy';

export async function GET() {
  try {
    const r = await apiFetch('/api/v1/admin/access', {
      cache: 'no-store',
    });

    return new Response(r.body, {
      status: r.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch {
    return new Response(JSON.stringify({ error: 'admin access backend unavailable' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

export async function POST(request: Request) {
  const body = await request.json();

  try {
    const r = await apiFetch('/api/v1/admin/access', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      cache: 'no-store',
    });

    return new Response(r.body, {
      status: r.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch {
    return new Response(JSON.stringify({ error: 'admin access backend unavailable' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}
