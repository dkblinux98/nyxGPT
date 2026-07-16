import { apiFetch } from '@/lib/apiProxy';

export async function GET(request: Request) {
  const { search } = new URL(request.url);

  try {
    const res = await apiFetch(`/api/v1/self-heal/logs${search}`, {
      method: 'GET',
      cache: 'no-store',
    });

    const data = await res.json();

    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (error) {
    console.error('Failed to fetch component logs:', error);
    return new Response(JSON.stringify({ error: 'Failed to fetch component logs from backend' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}
