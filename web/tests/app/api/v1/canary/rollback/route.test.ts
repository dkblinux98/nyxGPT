/**
 * Tests for the /api/v1/canary/rollback Next.js proxy route (issue #3245, #3419).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { ok: boolean; status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

function req(body: unknown = {}) {
  return new Request('http://localhost/api/v1/canary/rollback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

describe('/api/v1/canary/rollback proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST request to backend using default URL', async () => {
    mockFetch({ ok: true, status: 200, data: { rolledBack: true } });

    const { POST } = await import('../../../../../../src/app/api/v1/canary/rollback/route');
    const response = (await POST(req({ component: 'web' }))) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/canary/rollback');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ component: 'web' });

    const body = await response.json();
    expect(body).toEqual({ rolledBack: true });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, data: { rolledBack: true } });

    const { POST } = await import('../../../../../../src/app/api/v1/canary/rollback/route');
    await POST(req());

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/canary/rollback');
  });

  it('falls back to an empty body when request JSON is invalid', async () => {
    mockFetch({ ok: true, status: 200, data: { rolledBack: true } });

    const { POST } = await import('../../../../../../src/app/api/v1/canary/rollback/route');
    const badReq = new Request('http://localhost/api/v1/canary/rollback', {
      method: 'POST',
      body: 'not json',
    });
    await POST(badReq);

    const calledOptions = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(JSON.parse(calledOptions.body)).toEqual({});
  });

  it('passes through backend response status', async () => {
    mockFetch({ ok: false, status: 409, data: { error: 'no active rollout' } });

    const { POST } = await import('../../../../../../src/app/api/v1/canary/rollback/route');
    const response = (await POST(req())) as Response;

    expect(response.status).toBe(409);
    const body = await response.json();
    expect(body).toEqual({ error: 'no active rollout' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import('../../../../../../src/app/api/v1/canary/rollback/route');
    const response = (await POST(req())) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to roll back canary rollout' });
  });

  it('returns 502 when the backend response body is not valid JSON', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.reject(new Error('invalid JSON')),
    });

    const { POST } = await import('../../../../../../src/app/api/v1/canary/rollback/route');
    const response = (await POST(req())) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to roll back canary rollout' });
  });
});
