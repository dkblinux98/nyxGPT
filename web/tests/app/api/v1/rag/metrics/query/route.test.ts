/**
 * Tests for the /api/v1/rag/metrics/query Next.js proxy route (issue #3444).
 *
 * See web/tests/app/api/v1/rag/query/route.test.ts for the companion
 * non-metrics route and the rationale for restoring the server-side proxy.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { ok: boolean; status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

describe('/api/v1/rag/metrics/query proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST body to backend using default URL', async () => {
    mockFetch({ ok: true, status: 200, data: { results: [], evaluation_metrics: null } });

    const { POST } = await import('../../../../../../../src/app/api/v1/rag/metrics/query/route');
    const payload = { query: 'hello', top_k: 5, debug_mode: true, collect_metrics: true };
    const request = new Request('http://localhost/api/v1/rag/metrics/query', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    const response = (await POST(request)) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/rag/metrics/query');
    expect(calledOptions.method).toBe('POST');
    expect(calledOptions.body).toBe(JSON.stringify(payload));

    const body = await response.json();
    expect(body).toEqual({ results: [], evaluation_metrics: null });
    expect(response.status).toBe(200);
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, data: { results: [] } });

    const { POST } = await import('../../../../../../../src/app/api/v1/rag/metrics/query/route');
    const request = new Request('http://localhost/api/v1/rag/metrics/query', {
      method: 'POST',
      body: JSON.stringify({ query: 'hi' }),
    });
    await POST(request);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/rag/metrics/query');
  });

  it('passes through upstream error status and body', async () => {
    mockFetch({ ok: false, status: 500, data: { detail: 'index unavailable' } });

    const { POST } = await import('../../../../../../../src/app/api/v1/rag/metrics/query/route');
    const request = new Request('http://localhost/api/v1/rag/metrics/query', {
      method: 'POST',
      body: JSON.stringify({ query: 'hi' }),
    });
    const response = (await POST(request)) as Response;

    expect(response.status).toBe(500);
    const body = await response.json();
    expect(body).toEqual({ detail: 'index unavailable' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import('../../../../../../../src/app/api/v1/rag/metrics/query/route');
    const request = new Request('http://localhost/api/v1/rag/metrics/query', {
      method: 'POST',
      body: JSON.stringify({ query: 'hi' }),
    });
    const response = (await POST(request)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to run RAG metrics query' });
  });
});
