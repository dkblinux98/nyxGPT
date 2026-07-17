/**
 * Tests for the /api/v1/monitoring Next.js proxy route (issue #3245).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { status: number; body?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    status: opts.status,
    body: opts.body ?? null,
  });
}

describe('/api/v1/monitoring proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to backend using default URL and streams the body through', async () => {
    mockFetch({ status: 200, body: JSON.stringify({ status: 'ok' }) });

    const { GET } = await import('../../../../../src/app/api/v1/monitoring/route');
    const response = (await GET()) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/monitoring');
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
    const body = await response.json();
    expect(body).toEqual({ status: 'ok' });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ status: 200 });

    const { GET } = await import('../../../../../src/app/api/v1/monitoring/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/monitoring');
  });

  it('passes through a non-2xx backend status', async () => {
    mockFetch({ status: 503 });

    const { GET } = await import('../../../../../src/app/api/v1/monitoring/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(503);
  });

  it('returns 502 with a structured error when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import('../../../../../src/app/api/v1/monitoring/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(502);
    expect(response.headers.get('Content-Type')).toBe('application/json');
    const body = await response.json();
    expect(body).toEqual({ error: 'monitoring backend unavailable' });
  });
});
