/**
 * Tests for the /api/v1/admin/workflow-analytics Next.js proxy route (issue #3245).
 *
 * web/src/app/api/v1/admin/workflow-analytics/route.ts forwards GET (with
 * query string) to the backend /api/v1/admin/workflow-analytics endpoint via
 * apiFetch.
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

describe('/api/v1/admin/workflow-analytics GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set and no query', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/admin/workflow-analytics/route'
    );
    await GET(new Request('http://localhost/api/v1/admin/workflow-analytics'));

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/admin/workflow-analytics');
  });

  it('forwards the query string to the backend', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/admin/workflow-analytics/route'
    );
    await GET(
      new Request(
        'http://localhost/api/v1/admin/workflow-analytics?window=7d'
      )
    );

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe(
      'http://127.0.0.1:8000/api/v1/admin/workflow-analytics?window=7d'
    );
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/admin/workflow-analytics/route'
    );
    await GET(new Request('http://localhost/api/v1/admin/workflow-analytics'));

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe(
      'http://custom-backend:9000/api/v1/admin/workflow-analytics'
    );
  });

  it('passes through a successful backend response status', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/admin/workflow-analytics/route'
    );
    const response = (await GET(
      new Request('http://localhost/api/v1/admin/workflow-analytics')
    )) as Response;

    expect(response.status).toBe(200);
  });

  it('passes through a non-2xx backend response status', async () => {
    mockFetch({ ok: false, status: 404 });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/admin/workflow-analytics/route'
    );
    const response = (await GET(
      new Request('http://localhost/api/v1/admin/workflow-analytics')
    )) as Response;

    expect(response.status).toBe(404);
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import(
      '../../../../../../src/app/api/v1/admin/workflow-analytics/route'
    );
    const response = (await GET(
      new Request('http://localhost/api/v1/admin/workflow-analytics')
    )) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'workflow analytics backend unavailable' });
  });
});
