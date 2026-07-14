import { NextRequest } from 'next/server';

const BASE_URL = process.env.NYXGPT_API_BASE_URL ?? 'http://127.0.0.1:8000';

export async function GET(request: NextRequest) {
  const { search } = new URL(request.url);

  try {
    const r = await fetch(`${BASE_URL}/api/v1/admin/workflow-analytics${search}`, {
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
