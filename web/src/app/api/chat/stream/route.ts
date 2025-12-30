import { NextRequest } from 'next/server';
import { Agent } from 'undici';

const API_BASE = process.env.MYGPT_API_BASE ?? 'http://127.0.0.1:8000';

// Undici defaults can abort long-lived streaming responses.
// Configure a local agent with a very large body timeout.
const agent = new Agent({
  // 0 disables the body timeout entirely.
  bodyTimeout: 0,
  headersTimeout: 60_000,
});

export async function POST(req: NextRequest) {
  const body = await req.json();

  const upstream = await fetch(`${API_BASE}/api/v1/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    // @ts-expect-error - Next/undici fetch supports dispatcher
    dispatcher: agent,
    cache: 'no-store',
  });

  if (!upstream.ok || !upstream.body) {
    let detail = '';
    try {
      detail = await upstream.text();
    } catch {
      // ignore
    }

    return new Response(
      JSON.stringify({ error: 'Upstream chat stream failed', detail }),
      { status: 502, headers: { 'Content-Type': 'application/json' } }
    );
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      'Content-Type': 'text/plain; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform, no-store',
    },
  });
}