/**
 * Tests for the /api/v1/self-heal/toggle Next.js proxy route (issue #3245).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { ok: boolean; status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

describe('/api/v1/self-heal/toggle proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST body to backend using default URL', async () => {
    mockFetch({ ok: true, status: 200, data: { enabled: false } });

    const { POST } = await import('../../../../../../src/app/api/v1/self-heal/toggle/route');
    const req = new Request('http://localhost/api/v1/self-heal/toggle', {
      method: 'POST',
      body: JSON.stringify({ enabled: false }),
    });
    const response = (await POST(req)) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/self-heal/toggle');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ enabled: false });

    const body = await response.json();
    expect(body).toEqual({ enabled: false });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, data: { enabled: true } });

    const { POST } = await import('../../../../../../src/app/api/v1/self-heal/toggle/route');
    const req = new Request('http://localhost/api/v1/self-heal/toggle', {
      method: 'POST',
      body: JSON.stringify({ enabled: true }),
    });
    await POST(req);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/self-heal/toggle');
  });

  it('falls back to an empty body when request JSON is invalid', async () => {
    mockFetch({ ok: true, status: 200, data: { enabled: true } });

    const { POST } = await import('../../../../../../src/app/api/v1/self-heal/toggle/route');
    const req = new Request('http://localhost/api/v1/self-heal/toggle', {
      method: 'POST',
      body: 'not json',
    });
    await POST(req);

    const calledOptions = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(JSON.parse(calledOptions.body)).toEqual({});
  });

  it('passes through backend response status', async () => {
    mockFetch({ ok: false, status: 400, data: { error: 'bad request' } });

    const { POST } = await import('../../../../../../src/app/api/v1/self-heal/toggle/route');
    const req = new Request('http://localhost/api/v1/self-heal/toggle', {
      method: 'POST',
      body: JSON.stringify({}),
    });
    const response = (await POST(req)) as Response;

    expect(response.status).toBe(400);
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import('../../../../../../src/app/api/v1/self-heal/toggle/route');
    const req = new Request('http://localhost/api/v1/self-heal/toggle', {
      method: 'POST',
      body: JSON.stringify({}),
    });
    const response = (await POST(req)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to toggle self-heal' });
  });
});
