/**
 * Tests for the /api/v1/self-heal/logs Next.js proxy route (issue #3245).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { ok: boolean; status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

describe('/api/v1/self-heal/logs proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('uses default backend URL and forwards query string', async () => {
    mockFetch({ ok: true, status: 200, data: { logs: [] } });

    const { GET } = await import('../../../../../../src/app/api/v1/self-heal/logs/route');
    const response = (await GET(
      new Request('http://localhost/api/v1/self-heal/logs?component=api&lines=50')
    )) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe(
      'http://127.0.0.1:8000/api/v1/self-heal/logs?component=api&lines=50'
    );
    const body = await response.json();
    expect(body).toEqual({ logs: [] });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, data: { logs: [] } });

    const { GET } = await import('../../../../../../src/app/api/v1/self-heal/logs/route');
    await GET(new Request('http://localhost/api/v1/self-heal/logs?component=api'));

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/self-heal/logs?component=api');
  });

  it('forwards request with no query string unchanged', async () => {
    mockFetch({ ok: true, status: 200, data: { logs: [] } });

    const { GET } = await import('../../../../../../src/app/api/v1/self-heal/logs/route');
    await GET(new Request('http://localhost/api/v1/self-heal/logs'));

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/self-heal/logs');
  });

  it('passes through backend response status', async () => {
    mockFetch({ ok: false, status: 404, data: { error: 'component not found' } });

    const { GET } = await import('../../../../../../src/app/api/v1/self-heal/logs/route');
    const response = (await GET(
      new Request('http://localhost/api/v1/self-heal/logs?component=missing')
    )) as Response;

    expect(response.status).toBe(404);
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import('../../../../../../src/app/api/v1/self-heal/logs/route');
    const response = (await GET(
      new Request('http://localhost/api/v1/self-heal/logs')
    )) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to fetch component logs from backend' });
  });
});
