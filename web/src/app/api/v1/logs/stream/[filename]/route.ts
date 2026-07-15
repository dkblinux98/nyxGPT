import { NextRequest } from 'next/server';
import { apiFetch } from '@/lib/apiProxy';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ filename: string }> }
) {
  const { filename } = await params;

  const searchParams = request.nextUrl.searchParams;

  // Forward query parameters (level, search)
  const queryString = searchParams.toString();
  const path = queryString
    ? `/api/v1/logs/stream/${encodeURIComponent(filename)}?${queryString}`
    : `/api/v1/logs/stream/${encodeURIComponent(filename)}`;

  const r = await apiFetch(path, {
    cache: "no-store",
  });

  return new Response(r.body, {
    status: r.status,
    headers: {
      "Content-Type": r.headers.get("Content-Type") || "text/plain",
      "Content-Disposition": r.headers.get("Content-Disposition") || `inline; filename="${filename}"`,
      "X-Content-Type-Options": "nosniff",
    },
  });
}
