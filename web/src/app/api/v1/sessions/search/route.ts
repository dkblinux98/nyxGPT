import { NextRequest } from 'next/server';

const getBaseUrl = () =>
  process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";

export async function GET(request: NextRequest) {
  const searchParams = request.nextUrl.searchParams;

  // Forward all query parameters
  const queryString = searchParams.toString();
  const url = queryString
    ? `${getBaseUrl()}/api/v1/sessions/search?${queryString}`
    : `${getBaseUrl()}/api/v1/sessions/search`;

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
