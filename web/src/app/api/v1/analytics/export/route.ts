import { NextRequest } from 'next/server';

const BASE_URL = process.env.NYXGPT_API_BASE_URL ?? 'http://127.0.0.1:8000';

export async function GET(request: NextRequest) {
  const { search } = new URL(request.url);

  try {
    const r = await fetch(`${BASE_URL}/api/v1/analytics/export${search}`, {
      cache: 'no-store',
    });

    const headers: Record<string, string> = {
      'Content-Type': r.headers.get('Content-Type') ?? 'application/octet-stream',
    };
    const contentDisposition = r.headers.get('Content-Disposition');
    if (contentDisposition) {
      headers['Content-Disposition'] = contentDisposition;
    }

    return new Response(r.body, { status: r.status, headers });
  } catch {
    return new Response(JSON.stringify({ error: 'usage analytics backend unavailable' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}
