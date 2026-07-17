/**
 * Tests for the /api/info Next.js proxy route.
 *
 * Issue #3245: bring vitest coverage for API proxy routes to 100%. This
 * route has no try/catch — GET just forwards to the backend and passes
 * through status/body.
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

describe('/api/info GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to backend info endpoint', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../src/app/api/info/route');
    await GET();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/info');
    expect(calledOptions.cache).toBe('no-store');
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../src/app/api/info/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/info');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../src/app/api/info/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/info');
  });

  it('passes through backend response status', async () => {
    mockFetch({ ok: false, status: 500 });

    const { GET } = await import('../../../../src/app/api/info/route');
    const response = await GET() as Response;

    expect(response.status).toBe(500);
    expect(response.headers.get('Content-Type')).toBe('application/json');
  });
});
