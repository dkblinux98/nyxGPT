import { apiFetch } from '@/lib/apiProxy';

export async function POST(request: Request) {
  const body = await request.json().catch(() => ({}));

  try {
    const r = await apiFetch('/api/v1/infra/restart-required', {
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
    return new Response(JSON.stringify({ error: 'infra backend unavailable' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}
