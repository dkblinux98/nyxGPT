import { apiFetch } from '@/lib/apiProxy';

export async function GET(request: Request) {
  const { search } = new URL(request.url);

  try {
    const r = await apiFetch(`/api/v1/admin/activity${search}`, {
      cache: 'no-store',
    });

    return new Response(r.body, {
      status: r.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch {
    return new Response(JSON.stringify({ error: 'admin activity backend unavailable' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}
