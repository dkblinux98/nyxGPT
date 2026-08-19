/**
 * Tests for the /api/v1/support/context Next.js proxy route (#3745, #3811).
 *
 * This is what the Support menu reads before it offers anything: the running
 * environment, the prefilled GitHub form, and whether this install can file
 * a ticket itself. Reading that must not change anything, so this route is
 * GET-only -- filing lives at `/api/v1/support/tickets` and is the single
 * write on the surface. (It was GET-only for a stronger reason until #3811:
 * nyxGPT filed nothing at all. The owner rejected that in acceptance, since
 * it meant handing a user with a broken install to GitHub's compose page.)
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

  it('exposes no mutating handler -- reading the context changes nothing', async () => {
    const route = await import(ROUTE);

    expect(route.GET).toBeDefined();
    expect(route.POST).toBeUndefined();
    expect(route.PUT).toBeUndefined();
    expect(route.PATCH).toBeUndefined();
    expect(route.DELETE).toBeUndefined();
  });
});
