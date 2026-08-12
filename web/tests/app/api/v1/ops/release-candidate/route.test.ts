/**
 * Tests for the /api/v1/ops/release-candidate Next.js proxy route (#3727).
 *
 * The route is GET-only on purpose -- publishing to PyPI runs in the
 * schedule/dispatch-only workflow and in `nyxgpt release publish --publish`,
 * never behind a button a browser session could press. These tests pin that:
 * the backend's status and body pass through untouched, `?branch=` and
 * `?channel=` reach the backend (the documented API accepts both), an
 * unreachable backend degrades to a structured 502 rather than a stack
 * trace, and no request method other than GET is exported.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const ROUTE = '../../../../../../src/app/api/v1/ops/release-candidate/route';

function mockFetch(opts: { status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.status < 400,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

function req(query = '') {
  return new Request(`http://localhost/api/v1/ops/release-candidate${query}`);
}

function calledUrl(): string {
  return (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
}

describe('/api/v1/ops/release-candidate proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to the backend default URL and returns the plan', async () => {
    mockFetch({ status: 200, data: { channel: 'rc', version: '3.0.0rc2', publishable: true } });

    const { GET } = await import(ROUTE);
    const response = (await GET(req())) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, options] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe('http://127.0.0.1:8000/api/v1/ops/release-candidate');
    expect(options.method).toBe('GET');
    // Never cached: the plan is derived from what PyPI serves right now, and
    // a stale answer would propose a version that is already taken.
    expect(options.cache).toBe('no-store');

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
    expect(await response.json()).toEqual({
      channel: 'rc',
      version: '3.0.0rc2',
      publishable: true,
    });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ status: 200 });

    const { GET } = await import(ROUTE);
    await GET(req());

    expect(calledUrl()).toBe('http://custom-backend:9000/api/v1/ops/release-candidate');
  });

  it('forwards the branch and channel the caller asked for', async () => {
    mockFetch({ status: 200 });

    const { GET } = await import(ROUTE);
    await GET(req('?branch=v3.0.0&channel=dev'));

    expect(calledUrl()).toBe(
      'http://127.0.0.1:8000/api/v1/ops/release-candidate?branch=v3.0.0&channel=dev'
    );
  });

  it('forwards nothing it was not given -- the backend picks the defaults', async () => {
    mockFetch({ status: 200 });

    const { GET } = await import(ROUTE);
    await GET(req('?unrelated=1'));

    expect(calledUrl()).toBe('http://127.0.0.1:8000/api/v1/ops/release-candidate');
  });

  it('passes a non-2xx backend status and body straight through', async () => {
    mockFetch({ status: 400, data: { error: { message: "Unknown channel 'nightly'" } } });

    const { GET } = await import(ROUTE);
    const response = (await GET(req('?channel=nightly'))) as Response;

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: { message: "Unknown channel 'nightly'" } });
  });

  it('returns a structured 502 when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import(ROUTE);
    const response = (await GET(req())) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      error: 'Failed to fetch the release-candidate plan from backend',
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
      error: 'Failed to fetch the release-candidate plan from backend',
    });
  });

  it('tags the response with a correlation id', async () => {
    mockFetch({ status: 200 });

    const { GET } = await import(ROUTE);
    const response = (await GET(req())) as Response;

    expect(response.headers.get('X-Request-Id')).toBeTruthy();
  });

  it('exposes no mutating handler -- publishing is never a browser action', async () => {
    const route = await import(ROUTE);

    expect(route.GET).toBeDefined();
    expect(route.POST).toBeUndefined();
    expect(route.PUT).toBeUndefined();
    expect(route.PATCH).toBeUndefined();
    expect(route.DELETE).toBeUndefined();
  });
});
