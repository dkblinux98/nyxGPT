/**
 * Tests for the /api/v1/logs/stream/[filename] Next.js proxy route
 * (issue #3245).
 *
 * Branches exercised:
 *  - queryString ternary: with and without forwarded query params.
 *  - Content-Type fallback: backend header present vs. absent ("text/plain").
 *  - Content-Disposition fallback: backend header present vs. absent
 *    (`inline; filename="<filename>"`).
 * No try/catch exists in the handler, so there is no error branch to
 * exercise.
 */
import { NextRequest } from 'next/server';
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: {
  ok: boolean;
  status: number;
  body?: string | null;
  headers?: Record<string, string>;
}) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    body: opts.body ?? null,
    headers: new Headers(opts.headers ?? {}),
  });
}

describe('/api/v1/logs/stream/[filename] GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET with no query params using the default URL and falls back on missing headers', async () => {
    mockFetch({ ok: true, status: 200, body: 'log line 1\nlog line 2' });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/logs/stream/[filename]/route'
    );
    const request = new NextRequest('http://localhost/api/v1/logs/stream/app.log');
    const response = (await GET(request, {
      params: Promise.resolve({ filename: 'app.log' }),
    })) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/logs/stream/app.log');
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('text/plain');
    expect(response.headers.get('Content-Disposition')).toBe('inline; filename="app.log"');
    expect(response.headers.get('X-Content-Type-Options')).toBe('nosniff');
    const body = await response.text();
    expect(body).toBe('log line 1\nlog line 2');
  });

  it('forwards query params (level, search) using a custom backend URL and passes through backend headers', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({
      ok: true,
      status: 200,
      headers: {
        'Content-Type': 'application/octet-stream',
        'Content-Disposition': 'attachment; filename="renamed.log"',
      },
    });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/logs/stream/[filename]/route'
    );
    const request = new NextRequest(
      'http://localhost/api/v1/logs/stream/app.log?level=error&search=timeout'
    );
    const response = (await GET(request, {
      params: Promise.resolve({ filename: 'app.log' }),
    })) as Response;

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe(
      'http://custom-backend:9000/api/v1/logs/stream/app.log?level=error&search=timeout'
    );

    expect(response.headers.get('Content-Type')).toBe('application/octet-stream');
    expect(response.headers.get('Content-Disposition')).toBe('attachment; filename="renamed.log"');
  });

  it('URL-encodes a filename containing special characters', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/logs/stream/[filename]/route'
    );
    const request = new NextRequest('http://localhost/api/v1/logs/stream/my%20file.log');
    const response = (await GET(request, {
      params: Promise.resolve({ filename: 'my file.log' }),
    })) as Response;

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/logs/stream/my%20file.log');
    // Fallback Content-Disposition uses the raw (decoded) filename, not the
    // URL-encoded one used in the outbound request.
    expect(response.headers.get('Content-Disposition')).toBe('inline; filename="my file.log"');
  });

  it('passes through a non-2xx backend status', async () => {
    mockFetch({ ok: false, status: 404, body: JSON.stringify({ error: 'not found' }) });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/logs/stream/[filename]/route'
    );
    const request = new NextRequest('http://localhost/api/v1/logs/stream/missing.log');
    const response = (await GET(request, {
      params: Promise.resolve({ filename: 'missing.log' }),
    })) as Response;

    expect(response.status).toBe(404);
  });
});
