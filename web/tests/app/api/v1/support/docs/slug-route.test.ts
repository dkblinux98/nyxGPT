/**
 * Tests for the /api/v1/support/docs/[slug] Next.js proxy route (#3745).
 *
 * The slug is passed through url-encoded and left for the backend to judge --
 * `nyxgpt.support` is the single place that decides what names a packaged
 * document, and it rejects anything that could reach outside the packaged
 * docs directory. The behaviour that matters here is that a backend 404 stays
 * a 404: the viewer distinguishes "no such document" from "backend down", and
 * flattening the former into a 502 would show the wrong message.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const ROUTE = '../../../../../../src/app/api/v1/support/docs/[slug]/route';

function mockFetch(opts: { status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.status < 400,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

function req() {
  return new Request('http://localhost/api/v1/support/docs/configuration');
}

function ctx(slug: string) {
  return { params: Promise.resolve({ slug }) };
}

describe('/api/v1/support/docs/[slug] proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET for the named document and returns the rendered HTML', async () => {
    mockFetch({
      status: 200,
      data: { slug: 'configuration', title: 'Configuration', html: '<h1>Configuration</h1>' },
    });

    const { GET } = await import(ROUTE);
    const response = (await GET(req(), ctx('configuration'))) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/support/docs/configuration');
    expect(calledOptions.method).toBe('GET');
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
    expect((await response.json()).html).toBe('<h1>Configuration</h1>');
  });

  it('url-encodes the slug rather than splicing it into the upstream path', async () => {
    mockFetch({ status: 404, data: { detail: 'Unknown document' } });

    const { GET } = await import(ROUTE);
    await GET(req(), ctx('../secrets'));

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/support/docs/..%2Fsecrets');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ status: 200 });

    const { GET } = await import(ROUTE);
    await GET(req(), ctx('rag'));

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/support/docs/rag');
  });

  it('forwards a 404 for an unknown document instead of flattening it to 502', async () => {
    mockFetch({ status: 404, data: { detail: 'Unknown document: nope' } });

    const { GET } = await import(ROUTE);
    const response = (await GET(req(), ctx('nope'))) as Response;

    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ detail: 'Unknown document: nope' });
  });

  it('returns a structured 502 when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import(ROUTE);
    const response = (await GET(req(), ctx('configuration'))) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      error: 'Failed to fetch the document from backend',
    });
  });

  it('tags the response with a correlation id', async () => {
    mockFetch({ status: 200 });

    const { GET } = await import(ROUTE);
    const response = (await GET(req(), ctx('configuration'))) as Response;

    expect(response.headers.get('X-Request-Id')).toBeTruthy();
  });

  it('exposes no mutating handler -- the docs surface is read-only', async () => {
    const route = await import(ROUTE);

    expect(route.GET).toBeDefined();
    expect(route.POST).toBeUndefined();
    expect(route.PUT).toBeUndefined();
    expect(route.PATCH).toBeUndefined();
    expect(route.DELETE).toBeUndefined();
  });
});
