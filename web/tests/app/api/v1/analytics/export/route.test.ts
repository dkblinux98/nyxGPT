/**
 * Tests for the /api/v1/analytics/export Next.js proxy route (issue #3245).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: {
  ok: boolean;
  status: number;
  body?: unknown;
  headers?: Record<string, string>;
}) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    body: opts.body !== undefined ? opts.body : null,
    headers: new Headers(opts.headers ?? {}),
  });
}

describe('/api/v1/analytics/export proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('uses default backend URL and forwards query string', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/analytics/export/route');
    await GET(new Request('http://localhost/api/v1/analytics/export?format=csv'));

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/analytics/export?format=csv');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/analytics/export/route');
    await GET(new Request('http://localhost/api/v1/analytics/export?format=csv'));

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/analytics/export?format=csv');
  });

  it('forwards request with no query string unchanged', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/analytics/export/route');
    await GET(new Request('http://localhost/api/v1/analytics/export'));

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/analytics/export');
  });

  it('defaults Content-Type to application/octet-stream when backend omits it', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/analytics/export/route');
    const response = (await GET(
      new Request('http://localhost/api/v1/analytics/export')
    )) as Response;

    expect(response.headers.get('Content-Type')).toBe('application/octet-stream');
    expect(response.headers.get('Content-Disposition')).toBeNull();
  });

  it('passes through backend Content-Type and Content-Disposition headers', async () => {
    mockFetch({
      ok: true,
      status: 200,
      headers: {
        'Content-Type': 'text/csv',
        'Content-Disposition': 'attachment; filename="export.csv"',
      },
    });

    const { GET } = await import('../../../../../../src/app/api/v1/analytics/export/route');
    const response = (await GET(
      new Request('http://localhost/api/v1/analytics/export')
    )) as Response;

    expect(response.headers.get('Content-Type')).toBe('text/csv');
    expect(response.headers.get('Content-Disposition')).toBe('attachment; filename="export.csv"');
  });

  it('passes through backend response status', async () => {
    mockFetch({ ok: false, status: 404 });

    const { GET } = await import('../../../../../../src/app/api/v1/analytics/export/route');
    const response = (await GET(
      new Request('http://localhost/api/v1/analytics/export')
    )) as Response;

    expect(response.status).toBe(404);
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import('../../../../../../src/app/api/v1/analytics/export/route');
    const response = (await GET(
      new Request('http://localhost/api/v1/analytics/export')
    )) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'usage analytics backend unavailable' });
  });
});
