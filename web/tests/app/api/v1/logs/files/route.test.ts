/**
 * Tests for the /api/v1/logs/files Next.js proxy route (issue #3245).
 *
 * The route is a straight-line proxy (no branches): GET() always calls
 * apiFetch('/api/v1/logs/files') and mirrors the backend's status/body,
 * with a hardcoded 'application/json' Content-Type. No try/catch exists in
 * the handler, so there is no error branch to exercise.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { ok: boolean; status: number; body?: string | null }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    body: opts.body ?? null,
    headers: new Headers({ 'Content-Type': 'application/json' }),
  });
}

describe('/api/v1/logs/files GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to backend logs/files endpoint using default URL', async () => {
    mockFetch({ ok: true, status: 200, body: JSON.stringify({ files: ['app.log'] }) });

    const { GET } = await import('../../../../../../src/app/api/v1/logs/files/route');
    const response = (await GET()) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/logs/files');
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
    const body = await response.text();
    expect(body).toBe(JSON.stringify({ files: ['app.log'] }));
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/logs/files/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/logs/files');
  });

  it('passes through a non-2xx backend status', async () => {
    mockFetch({ ok: false, status: 500, body: JSON.stringify({ error: 'boom' }) });

    const { GET } = await import('../../../../../../src/app/api/v1/logs/files/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(500);
    const body = await response.json();
    expect(body).toEqual({ error: 'boom' });
  });
});
