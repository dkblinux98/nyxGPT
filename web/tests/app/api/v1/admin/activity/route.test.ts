/**
 * Tests for the /api/v1/admin/activity Next.js proxy route (issue #3245).
 *
 * web/src/app/api/v1/admin/activity/route.ts forwards GET (with query string)
 * to the backend /api/v1/admin/activity endpoint via apiFetch.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(response: { ok: boolean; status: number }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: response.ok,
    status: response.status,
    body: null,
    headers: new Headers({ 'Content-Type': 'application/json' }),
  });
}

describe('/api/v1/admin/activity GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set and no query', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/admin/activity/route');
    await GET(new Request('http://localhost/api/v1/admin/activity'));

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/admin/activity');
  });

  it('forwards the query string to the backend', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/admin/activity/route');
    await GET(
      new Request('http://localhost/api/v1/admin/activity?limit=10&offset=20')
    );

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe(
      'http://127.0.0.1:8000/api/v1/admin/activity?limit=10&offset=20'
    );
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/admin/activity/route');
    await GET(new Request('http://localhost/api/v1/admin/activity?limit=5'));

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/admin/activity?limit=5');
  });

  it('passes through a successful backend response status', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/admin/activity/route');
    const response = (await GET(
      new Request('http://localhost/api/v1/admin/activity')
    )) as Response;

    expect(response.status).toBe(200);
  });

  it('passes through a non-2xx backend response status', async () => {
    mockFetch({ ok: false, status: 500 });

    const { GET } = await import('../../../../../../src/app/api/v1/admin/activity/route');
    const response = (await GET(
      new Request('http://localhost/api/v1/admin/activity')
    )) as Response;

    expect(response.status).toBe(500);
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import('../../../../../../src/app/api/v1/admin/activity/route');
    const response = (await GET(
      new Request('http://localhost/api/v1/admin/activity')
    )) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'admin activity backend unavailable' });
  });
});
