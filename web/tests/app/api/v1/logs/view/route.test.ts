/**
 * Tests for the /api/v1/logs/view/[filename] Next.js proxy route
 * (issue #3245).
 *
 * Branch exercised: the queryString ternary, with and without forwarded
 * query params (tail, level, search). Content-Type is always hardcoded to
 * 'application/json' (no fallback branch). No try/catch exists in the
 * handler, so there is no error branch to exercise.
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

describe('/api/v1/logs/view/[filename] GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET with no query params to backend using default URL', async () => {
    mockFetch({ ok: true, status: 200, body: JSON.stringify({ lines: ['hello'] }) });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/logs/view/[filename]/route'
    );
    const request = new NextRequest('http://localhost/api/v1/logs/view/app.log');
    const response = (await GET(request, {
      params: Promise.resolve({ filename: 'app.log' }),
    })) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/logs/view/app.log');
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
    const body = await response.json();
    expect(body).toEqual({ lines: ['hello'] });
  });

  it('forwards query params (tail, level, search) using a custom backend URL', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/logs/view/[filename]/route'
    );
    const request = new NextRequest(
      'http://localhost/api/v1/logs/view/app.log?tail=100&level=warn&search=oops'
    );
    await GET(request, { params: Promise.resolve({ filename: 'app.log' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe(
      'http://custom-backend:9000/api/v1/logs/view/app.log?tail=100&level=warn&search=oops'
    );
  });

  it('URL-encodes a filename containing special characters', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/logs/view/[filename]/route'
    );
    const request = new NextRequest('http://localhost/api/v1/logs/view/my%20file.log');
    await GET(request, { params: Promise.resolve({ filename: 'my file.log' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/logs/view/my%20file.log');
  });

  it('passes through a non-2xx backend status', async () => {
    mockFetch({ ok: false, status: 500, body: JSON.stringify({ error: 'boom' }) });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/logs/view/[filename]/route'
    );
    const request = new NextRequest('http://localhost/api/v1/logs/view/app.log');
    const response = (await GET(request, {
      params: Promise.resolve({ filename: 'app.log' }),
    })) as Response;

    expect(response.status).toBe(500);
  });
});
