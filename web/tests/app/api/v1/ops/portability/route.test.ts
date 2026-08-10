/**
 * Tests for the /api/v1/ops/portability Next.js proxy route (P6-16, #3516).
 *
 * The route is GET-only on purpose -- the portability matrix describes the
 * product, not this machine, so there is nothing to act on from a browser.
 * These tests pin that: the backend's status and body pass through untouched,
 * an unreachable backend degrades to a structured 502 rather than a stack
 * trace, and no request method other than GET is exported.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const ROUTE = '../../../../../../src/app/api/v1/ops/portability/route';

function mockFetch(opts: { status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.status < 400,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

function req() {
  return new Request('http://localhost/api/v1/ops/portability');
}

describe('/api/v1/ops/portability proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to the backend default URL and returns the report', async () => {
    mockFetch({ status: 200, data: { acceptance_ready: false, summary: { total: 5 } } });

    const { GET } = await import(ROUTE);
    const response = (await GET(req())) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/ops/portability');
    expect(calledOptions.method).toBe('GET');
    // The matrix must never be served from a cache -- a stale row would
    // report a gap as closed (or vice versa).
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
    expect(await response.json()).toEqual({ acceptance_ready: false, summary: { total: 5 } });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ status: 200 });

    const { GET } = await import(ROUTE);
    await GET(req());

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/ops/portability');
  });

  it('passes a non-2xx backend status and body straight through', async () => {
    mockFetch({ status: 503, data: { detail: 'matrix unavailable' } });

    const { GET } = await import(ROUTE);
    const response = (await GET(req())) as Response;

    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ detail: 'matrix unavailable' });
  });

  it('returns a structured 502 when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import(ROUTE);
    const response = (await GET(req())) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      error: 'Failed to fetch the portability matrix from backend',
    });
  });

  it('returns a structured 502 when the backend body is not valid JSON', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.reject(new Error('invalid JSON')),
    });

    const { GET } = await import(ROUTE);
    const response = (await GET(req())) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      error: 'Failed to fetch the portability matrix from backend',
    });
  });

  it('tags the response with a correlation id', async () => {
    mockFetch({ status: 200 });

    const { GET } = await import(ROUTE);
    const response = (await GET(req())) as Response;

    expect(response.headers.get('X-Request-Id')).toBeTruthy();
  });

  it('exposes no mutating handler -- the matrix is read-only', async () => {
    const route = await import(ROUTE);

    expect(route.GET).toBeDefined();
    expect(route.POST).toBeUndefined();
    expect(route.PUT).toBeUndefined();
    expect(route.PATCH).toBeUndefined();
    expect(route.DELETE).toBeUndefined();
  });
});
