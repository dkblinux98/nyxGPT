/**
 * Tests for the /api/v1/rag/config Next.js proxy route (issue #3245).
 *
 * The handler is a straight-line GET proxy with no try/catch and no
 * branching: it always forwards to the backend and streams the response
 * body straight back. Full coverage requires exercising the default and
 * custom backend URL, plus a non-2xx status passthrough.
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

describe('/api/v1/rag/config GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to the backend using the default URL with no-store caching', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/rag/config/route');
    const response = (await GET()) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/rag/config');
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/rag/config/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/rag/config');
  });

  it('passes through a non-2xx backend status', async () => {
    mockFetch({ ok: false, status: 503 });

    const { GET } = await import('../../../../../../src/app/api/v1/rag/config/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(503);
  });
});
