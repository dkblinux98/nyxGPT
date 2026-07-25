/**
 * Tests for the /api/v1/rag/cache/stats Next.js proxy route (issue #3314).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { ok: boolean; status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

describe('/api/v1/rag/cache/stats proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200, data: { hits: 1, misses: 0, hit_rate: 1, size: 1 } });

    const { GET } = await import('../../../../../../../src/app/api/v1/rag/cache/stats/route');
    const response = (await GET()) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/rag/cache/stats');
    const body = await response.json();
    expect(body).toEqual({ hits: 1, misses: 0, hit_rate: 1, size: 1 });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, data: { hits: 0, misses: 0, hit_rate: 0, size: 0 } });

    const { GET } = await import('../../../../../../../src/app/api/v1/rag/cache/stats/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/rag/cache/stats');
  });

  it('passes through backend response status', async () => {
    mockFetch({ ok: false, status: 503, data: { error: 'unavailable' } });

    const { GET } = await import('../../../../../../../src/app/api/v1/rag/cache/stats/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(503);
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import('../../../../../../../src/app/api/v1/rag/cache/stats/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to fetch query cache stats from backend' });
  });
});
