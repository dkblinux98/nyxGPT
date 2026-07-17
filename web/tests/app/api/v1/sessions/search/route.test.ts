/**
 * Tests for the /api/v1/sessions/search Next.js proxy route (issue #3245).
 *
 * Branches exercised:
 *  - queryString ternary: with and without forwarded search query params.
 * No try/catch exists in the handler, so there is no error branch to
 * exercise.
 */
import { NextRequest } from 'next/server';
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { ok: boolean; status: number; body?: string | null }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    body: opts.body ?? null,
  });
}

describe('/api/v1/sessions/search GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards query params to the backend using the default URL', async () => {
    mockFetch({ ok: true, status: 200, body: JSON.stringify({ results: [] }) });

    const { GET } = await import('../../../../../../src/app/api/v1/sessions/search/route');
    const request = new NextRequest(
      'http://localhost/api/v1/sessions/search?q=hello&limit=10'
    );
    const response = (await GET(request)) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe(
      'http://127.0.0.1:8000/api/v1/sessions/search?q=hello&limit=10'
    );
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
  });

  it('forwards request with no query string unchanged (queryString empty branch)', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/sessions/search/route');
    const request = new NextRequest('http://localhost/api/v1/sessions/search');
    await GET(request);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/sessions/search');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/sessions/search/route');
    const request = new NextRequest('http://localhost/api/v1/sessions/search?q=hello');
    await GET(request);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/sessions/search?q=hello');
  });

  it('passes through a non-2xx backend status', async () => {
    mockFetch({ ok: false, status: 500, body: JSON.stringify({ error: 'search failed' }) });

    const { GET } = await import('../../../../../../src/app/api/v1/sessions/search/route');
    const request = new NextRequest('http://localhost/api/v1/sessions/search?q=hello');
    const response = (await GET(request)) as Response;

    expect(response.status).toBe(500);
  });
});
