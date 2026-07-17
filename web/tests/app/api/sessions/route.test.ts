/**
 * Tests for the /api/sessions Next.js proxy route.
 *
 * Issue #3245: bring vitest coverage for API proxy routes to 100%. This
 * route has no try/catch — GET forwards to the backend and adds explicit
 * no-cache headers on top of the standard Content-Type header.
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

describe('/api/sessions GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to backend sessions endpoint', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../src/app/api/sessions/route');
    await GET();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/sessions');
    expect(calledOptions.cache).toBe('no-store');
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../src/app/api/sessions/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/sessions');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../src/app/api/sessions/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/sessions');
  });

  it('passes through backend response status and sets no-cache headers', async () => {
    mockFetch({ ok: false, status: 503 });

    const { GET } = await import('../../../../src/app/api/sessions/route');
    const response = await GET() as Response;

    expect(response.status).toBe(503);
    expect(response.headers.get('Content-Type')).toBe('application/json');
    expect(response.headers.get('Cache-Control')).toBe('no-cache, no-store, must-revalidate');
    expect(response.headers.get('Pragma')).toBe('no-cache');
    expect(response.headers.get('Expires')).toBe('0');
  });
});
