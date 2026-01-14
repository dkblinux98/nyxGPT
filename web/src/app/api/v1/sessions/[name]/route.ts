import { NextRequest } from 'next/server';

const getBaseUrl = () =>
  process.env.MYGPT_API_BASE_URL ?? "http://127.0.0.1:8000";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;

  const r = await fetch(
    `${getBaseUrl()}/api/v1/sessions/${encodeURIComponent(name)}`,
    {
      cache: "no-store",
    }
  );

  return new Response(r.body, {
    status: r.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
