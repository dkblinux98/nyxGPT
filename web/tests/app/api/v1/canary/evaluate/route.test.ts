/**
 * Tests for the /api/v1/canary/evaluate Next.js proxy route (issue #3245, #3419).
 *
 * web/src/app/api/v1/canary/evaluate/route.ts forwards POST (JSON body, e.g.
 * {"component": "web"}) to the backend /api/v1/canary/evaluate endpoint via
 * apiFetch, re-serializing the parsed JSON response.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(response: { ok: boolean; status: number; json: object }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: response.ok,
    status: response.status,
    json: vi.fn().mockResolvedValue(response.json),
    headers: new Headers({ 'Content-Type': 'application/json' }),
  });
}

function req(body: unknown = {}) {
  return new Request('http://localhost/api/v1/canary/evaluate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

describe('/api/v1/canary/evaluate POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200, json: { evaluation: 'pass' } });

    const { POST } = await import('../../../../../../src/app/api/v1/canary/evaluate/route');
    await POST(req({ component: 'web' }));

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/canary/evaluate');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ component: 'web' });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, json: { evaluation: 'pass' } });

    const { POST } = await import('../../../../../../src/app/api/v1/canary/evaluate/route');
    await POST(req());

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/canary/evaluate');
  });

  it('passes through a successful backend response status and body', async () => {
    mockFetch({ ok: true, status: 200, json: { evaluation: 'pass', score: 0.98 } });

    const { POST } = await import('../../../../../../src/app/api/v1/canary/evaluate/route');
    const response = (await POST(req())) as Response;

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ evaluation: 'pass', score: 0.98 });
  });

  it('passes through a non-2xx backend response status', async () => {
    mockFetch({ ok: false, status: 409, json: { error: 'no active rollout' } });

    const { POST } = await import('../../../../../../src/app/api/v1/canary/evaluate/route');
    const response = (await POST(req())) as Response;

    expect(response.status).toBe(409);
    const body = await response.json();
    expect(body).toEqual({ error: 'no active rollout' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const { POST } = await import('../../../../../../src/app/api/v1/canary/evaluate/route');
    const response = (await POST(req())) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to evaluate canary metrics' });
    expect(consoleErrorSpy).toHaveBeenCalled();

    consoleErrorSpy.mockRestore();
  });
});
