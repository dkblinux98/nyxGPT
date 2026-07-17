/**
 * Tests for the /api/v1/deploy/status Next.js proxy route (issue #3245).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { ok: boolean; status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

describe('/api/v1/deploy/status proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to backend using default URL and returns 200 on success', async () => {
    mockFetch({ ok: true, status: 200, data: { activeColor: 'blue' } });

    const { GET } = await import('../../../../../../src/app/api/v1/deploy/status/route');
    const response = (await GET()) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/deploy/status');
    expect(calledOptions.method).toBe('GET');

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ activeColor: 'blue' });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, data: { activeColor: 'green' } });

    const { GET } = await import('../../../../../../src/app/api/v1/deploy/status/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/deploy/status');
  });

  it('passes through backend response status and body on non-2xx', async () => {
    mockFetch({ ok: false, status: 503, data: { error: 'status unavailable' } });

    const { GET } = await import('../../../../../../src/app/api/v1/deploy/status/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(503);
    const body = await response.json();
    expect(body).toEqual({ error: 'status unavailable' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import('../../../../../../src/app/api/v1/deploy/status/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to fetch deploy status from backend' });
  });

  it('returns 502 when the backend response body is not valid JSON', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.reject(new Error('invalid JSON')),
    });

    const { GET } = await import('../../../../../../src/app/api/v1/deploy/status/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to fetch deploy status from backend' });
  });
});
