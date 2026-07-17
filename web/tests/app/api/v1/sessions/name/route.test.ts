/**
 * Tests for the /api/v1/sessions/[name] Next.js proxy route (issue #3245).
 *
 * Branches exercised:
 *  - `if (offset)` / `if (limit)`: pagination params present vs. absent.
 *  - queryString ternary: with and without forwarded pagination params.
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

describe('/api/v1/sessions/[name] GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET with offset and limit query params using the default URL', async () => {
    mockFetch({ ok: true, status: 200, body: JSON.stringify({ name: 'my-session' }) });

    const { GET } = await import('../../../../../../src/app/api/v1/sessions/[name]/route');
    const request = new NextRequest(
      'http://localhost/api/v1/sessions/my-session?offset=10&limit=20'
    );
    const response = (await GET(request, {
      params: Promise.resolve({ name: 'my-session' }),
    })) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe(
      'http://127.0.0.1:8000/api/v1/sessions/my-session?offset=10&limit=20'
    );
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
  });

  it('forwards GET with no query params unchanged (queryString empty branch)', async () => {
    mockFetch({ ok: true, status: 200, body: JSON.stringify({ name: 'my-session' }) });

    const { GET } = await import('../../../../../../src/app/api/v1/sessions/[name]/route');
    const request = new NextRequest('http://localhost/api/v1/sessions/my-session');
    await GET(request, { params: Promise.resolve({ name: 'my-session' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/sessions/my-session');
  });

  it('forwards only offset when limit is absent', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/sessions/[name]/route');
    const request = new NextRequest('http://localhost/api/v1/sessions/my-session?offset=5');
    await GET(request, { params: Promise.resolve({ name: 'my-session' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/sessions/my-session?offset=5');
  });

  it('forwards only limit when offset is absent', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/sessions/[name]/route');
    const request = new NextRequest('http://localhost/api/v1/sessions/my-session?limit=5');
    await GET(request, { params: Promise.resolve({ name: 'my-session' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/sessions/my-session?limit=5');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/sessions/[name]/route');
    const request = new NextRequest('http://localhost/api/v1/sessions/my-session');
    await GET(request, { params: Promise.resolve({ name: 'my-session' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/sessions/my-session');
  });

  it('URL-encodes a session name containing special characters', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/sessions/[name]/route');
    const request = new NextRequest('http://localhost/api/v1/sessions/my%20session');
    await GET(request, { params: Promise.resolve({ name: 'my session' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/sessions/my%20session');
  });

  it('passes through a non-2xx backend status', async () => {
    mockFetch({ ok: false, status: 404, body: JSON.stringify({ error: 'not found' }) });

    const { GET } = await import('../../../../../../src/app/api/v1/sessions/[name]/route');
    const request = new NextRequest('http://localhost/api/v1/sessions/missing');
    const response = (await GET(request, {
      params: Promise.resolve({ name: 'missing' }),
    })) as Response;

    expect(response.status).toBe(404);
  });
});
