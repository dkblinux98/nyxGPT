/**
 * Tests for the /api/v1/metrics/history Next.js proxy route.
 *
 * Regression test for #3352: the Settings -> Resource Usage timeframe
 * buttons need a real server-side history endpoint. This proxy forwards
 * the `range` query param to the backend at /api/v1/metrics/history.
 */
import { NextRequest } from 'next/server';
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { ok: boolean; status: number; body?: string | null }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    body: opts.body ?? null,
    headers: new Headers({ 'Content-Type': 'application/json' }),
  });
}

describe('/api/v1/metrics/history proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards the range query param to the backend using the default URL', async () => {
    mockFetch({ ok: true, status: 200, body: JSON.stringify({ points: [] }) });

    const { GET } = await import('../../../../../../src/app/api/v1/metrics/history/route');
    const request = new NextRequest('http://localhost/api/v1/metrics/history?range=1h');
    const response = (await GET(request)) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/metrics/history?range=1h');
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/metrics/history/route');
    const request = new NextRequest('http://localhost/api/v1/metrics/history?range=24h');
    await GET(request);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/metrics/history?range=24h');
  });

  it('passes through a non-2xx backend status', async () => {
    mockFetch({ ok: false, status: 400, body: JSON.stringify({ error: 'invalid range' }) });

    const { GET } = await import('../../../../../../src/app/api/v1/metrics/history/route');
    const request = new NextRequest('http://localhost/api/v1/metrics/history?range=bogus');
    const response = (await GET(request)) as Response;

    expect(response.status).toBe(400);
  });

  it('returns 502 with a structured error when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import('../../../../../../src/app/api/v1/metrics/history/route');
    const request = new NextRequest('http://localhost/api/v1/metrics/history?range=7d');
    const response = (await GET(request)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'metrics history backend unavailable' });
  });
});
