import { NextRequest } from 'next/server';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ filename: string }> }
) {
  const { filename } = await params;

  const base =
    process.env.NYXGPT_API_BASE_URL ??
    "http://127.0.0.1:8000";
  const searchParams = request.nextUrl.searchParams;

  // Forward query parameters (tail, level, search)
  const queryString = searchParams.toString();
  const url = queryString
    ? `${base}/api/v1/logs/view/${encodeURIComponent(filename)}?${queryString}`
    : `${base}/api/v1/logs/view/${encodeURIComponent(filename)}`;

  const r = await fetch(url, {
    cache: "no-store",
  });

  return new Response(r.body, {
    status: r.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
