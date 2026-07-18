/**
 * Tests for the /api/v1/deploy/rollback Next.js proxy route (issue #3245).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { ok: boolean; status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

describe('/api/v1/deploy/rollback proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST to backend using default URL', async () => {
    mockFetch({ ok: true, status: 200, data: { rolledBack: true } });

    const { POST } = await import('../../../../../../src/app/api/v1/deploy/rollback/route');
    const response = (await POST()) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/deploy/rollback');
    expect(calledOptions.method).toBe('POST');

    const body = await response.json();
    expect(body).toEqual({ rolledBack: true });
    expect(response.status).toBe(200);
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, data: { rolledBack: true } });

    const { POST } = await import('../../../../../../src/app/api/v1/deploy/rollback/route');
    await POST();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/deploy/rollback');
  });

  it('passes through backend response status', async () => {
    mockFetch({ ok: false, status: 409, data: { error: 'rollback already in progress' } });

    const { POST } = await import('../../../../../../src/app/api/v1/deploy/rollback/route');
    const response = (await POST()) as Response;

    expect(response.status).toBe(409);
    const body = await response.json();
    expect(body).toEqual({ error: 'rollback already in progress' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import('../../../../../../src/app/api/v1/deploy/rollback/route');
    const response = (await POST()) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to roll back deployment' });
  });

  it('returns 502 when the backend response body is not valid JSON', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.reject(new Error('invalid JSON')),
    });

    const { POST } = await import('../../../../../../src/app/api/v1/deploy/rollback/route');
    const response = (await POST()) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to roll back deployment' });
  });
});
