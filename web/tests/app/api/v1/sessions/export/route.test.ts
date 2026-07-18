/**
 * Tests for the /api/v1/sessions/[name]/export Next.js proxy route
 * (issue #3245).
 *
 * Branches exercised:
 *  - `format` query param: explicit value vs. `?? 'markdown'` default.
 *  - Content-Type fallback: backend header present vs. `?? 'application/octet-stream'`.
 *  - `if (contentDisposition)`: backend header present vs. absent.
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

describe('/api/v1/sessions/[name]/export GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('defaults format to markdown and falls back on missing backend headers, using the default URL', async () => {
    mockFetch({ ok: true, status: 200, body: '# Session\n' });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/sessions/[name]/export/route'
    );
    const request = new NextRequest('http://localhost/api/v1/sessions/my-session/export');
    const response = (await GET(request, {
      params: Promise.resolve({ name: 'my-session' }),
    })) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe(
      'http://127.0.0.1:8000/api/v1/sessions/my-session/export?format=markdown'
    );
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/octet-stream');
    expect(response.headers.get('Content-Disposition')).toBeNull();
  });

  it('forwards an explicit format and passes through backend headers, using a custom URL', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({
      ok: true,
      status: 200,
      headers: {
        'Content-Type': 'application/json',
        'Content-Disposition': 'attachment; filename="my-session.json"',
      },
    });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/sessions/[name]/export/route'
    );
    const request = new NextRequest(
      'http://localhost/api/v1/sessions/my-session/export?format=json'
    );
    const response = (await GET(request, {
      params: Promise.resolve({ name: 'my-session' }),
    })) as Response;

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe(
      'http://custom-backend:9000/api/v1/sessions/my-session/export?format=json'
    );

    expect(response.headers.get('Content-Type')).toBe('application/json');
    expect(response.headers.get('Content-Disposition')).toBe(
      'attachment; filename="my-session.json"'
    );
  });

  it('URL-encodes a session name containing special characters', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/sessions/[name]/export/route'
    );
    const request = new NextRequest(
      'http://localhost/api/v1/sessions/my%20session/export'
    );
    await GET(request, { params: Promise.resolve({ name: 'my session' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe(
      'http://127.0.0.1:8000/api/v1/sessions/my%20session/export?format=markdown'
    );
  });

  it('passes through a non-2xx backend status', async () => {
    mockFetch({ ok: false, status: 404, body: JSON.stringify({ error: 'not found' }) });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/sessions/[name]/export/route'
    );
    const request = new NextRequest('http://localhost/api/v1/sessions/missing/export');
    const response = (await GET(request, {
      params: Promise.resolve({ name: 'missing' }),
    })) as Response;

    expect(response.status).toBe(404);
  });
});
