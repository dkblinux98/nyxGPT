import { NextRequest } from 'next/server';
import { apiFetch } from '@/lib/apiProxy';

export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;

  // Forward all query parameters
  const queryString = searchParams.toString();
  const path = queryString
    ? `/api/v1/sessions/search?${queryString}`
    : `/api/v1/sessions/search`;

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
