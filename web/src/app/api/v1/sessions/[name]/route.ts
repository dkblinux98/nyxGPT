import { NextRequest } from 'next/server';

const getBaseUrl = () =>
  process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;

  // Forward pagination query parameters
  const searchParams = request.nextUrl.searchParams;
  const offset = searchParams.get('offset');
  const limit = searchParams.get('limit');

  const url = new URL(
    `/api/v1/sessions/${encodeURIComponent(name)}`,
    getBaseUrl()
  );
  if (offset) url.searchParams.set('offset', offset);
  if (limit) url.searchParams.set('limit', limit);

  const r = await fetch(url.toString(), {
    cache: "no-store",
  });

  return new Response(r.body, {
    status: r.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
