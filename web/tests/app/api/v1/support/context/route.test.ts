/**
 * Tests for the /api/v1/support/context Next.js proxy route (#3745).
 *
 * This is the Support menu's environment + issue-form link. It is GET-only on
 * purpose: nyxGPT never files an issue on the user's behalf, so there is no
 * mutating counterpart anywhere under /support -- pinned here as well as on
 * the backend, since a POST added to this route would be the easiest way for
 * that invariant to quietly stop holding.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const ROUTE = '../../../../../../src/app/api/v1/support/context/route';

function mockFetch(opts: { status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.status < 400,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

function req() {
  return new Request('http://localhost/api/v1/support/context');
}

describe('/api/v1/support/context proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to the backend and returns the support context', async () => {
    mockFetch({
      status: 200,
      data: {
        environment: { version: '3.0.0', platform: 'Linux 6.1 (x86_64)', python: '3.12.1' },
        issue_form_url: 'https://github.com/dkblinux98/nyxGPT/issues/new?template=support.yml',
        requires_network: true,
      },
    });

    const { GET } = await import(ROUTE);
    const response = (await GET(req())) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/support/context');
    expect(calledOptions.method).toBe('GET');
    // The version reported here is the version running right now; a cached
    // answer would keep naming the previous one after an upgrade, and it is
    // what a support report gets stamped with.
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
    expect((await response.json()).environment.version).toBe('3.0.0');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ status: 200 });

    const { GET } = await import(ROUTE);
    await GET(req());

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/support/context');
  });

  it('passes a non-2xx backend status and body straight through', async () => {
    mockFetch({ status: 503, data: { detail: 'backend starting' } });

    const { GET } = await import(ROUTE);
    const response = (await GET(req())) as Response;

    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ detail: 'backend starting' });
  });

  it('returns a structured 502 when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import(ROUTE);
    const response = (await GET(req())) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      error: 'Failed to fetch the support context from backend',
    });
  });

  it('tags the response with a correlation id', async () => {
    mockFetch({ status: 200 });

    const { GET } = await import(ROUTE);
    const response = (await GET(req())) as Response;

    expect(response.headers.get('X-Request-Id')).toBeTruthy();
  });

  it('exposes no mutating handler -- nyxGPT never files on the user behalf', async () => {
    const route = await import(ROUTE);

    expect(route.GET).toBeDefined();
    expect(route.POST).toBeUndefined();
    expect(route.PUT).toBeUndefined();
    expect(route.PATCH).toBeUndefined();
    expect(route.DELETE).toBeUndefined();
  });
});
