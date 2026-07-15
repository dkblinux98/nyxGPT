import { NextRequest } from 'next/server';
import { apiFetch } from '@/lib/apiProxy';

export async function GET(request: NextRequest) {
  const { search } = new URL(request.url);

  try {
    const r = await apiFetch(`/api/v1/admin/workflow-analytics${search}`, {
      cache: 'no-store',
    });

    return new Response(r.body, {
      status: r.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch {
    return new Response(JSON.stringify({ error: 'workflow analytics backend unavailable' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}
