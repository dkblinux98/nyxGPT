import { NextRequest } from 'next/server';
import { apiFetch } from '@/lib/apiProxy';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;

  // Forward pagination query parameters
  const searchParams = request.nextUrl.searchParams;
  const offset = searchParams.get('offset');
  const limit = searchParams.get('limit');

  const query = new URLSearchParams();
  if (offset) query.set('offset', offset);
  if (limit) query.set('limit', limit);
  const queryString = query.toString();
  const path = `/api/v1/sessions/${encodeURIComponent(name)}${queryString ? `?${queryString}` : ''}`;

  const r = await apiFetch(path, {
    cache: "no-store",
  });

  return new Response(r.body, {
    status: r.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
