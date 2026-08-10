/**
 * Tests for the /api/v1/cloud/deploy/tunnel Next.js proxy route (#3513).
 *
 * The SSH tunnel is the only way anything on the instance is reachable, so
 * the requested action must reach the backend verbatim, and a failure to
 * open or close it must surface as an error rather than a quiet 200 that
 * would leave the dashboard claiming the wrong access state.
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
  return new Request('http://localhost/api/v1/cloud/deploy/tunnel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

describe('/api/v1/cloud/deploy/tunnel proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards the requested tunnel action to the backend', async () => {
    mockFetch({
      ok: true,
      status: 200,
      data: { action: 'tunnel', running: true, pid: 4242, urls: {} },
    });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/deploy/tunnel/route');
    const response = (await POST(req({ action: 'start' }), undefined)) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/cloud/deploy/tunnel');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ action: 'start' });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      action: 'tunnel',
      running: true,
      pid: 4242,
      urls: {},
    });
  });

  it('forwards a stop as faithfully as a start', async () => {
    mockFetch({ ok: true, status: 200, data: { action: 'tunnel-stop', stopped: true } });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/deploy/tunnel/route');
    await POST(req({ action: 'stop' }), undefined);

    const calledOptions = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(JSON.parse(calledOptions.body)).toEqual({ action: 'stop' });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/deploy/tunnel/route');
    await POST(req({ action: 'start' }), undefined);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/cloud/deploy/tunnel');
  });

  it('falls back to an empty body when the request JSON is invalid', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/deploy/tunnel/route');
    const badReq = new Request('http://localhost/api/v1/cloud/deploy/tunnel', {
      method: 'POST',
      body: 'not json',
    });
    await POST(badReq, undefined);

    const calledOptions = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(JSON.parse(calledOptions.body)).toEqual({});
  });

  it('passes through the backend status and body when the tunnel cannot open', async () => {
    mockFetch({ ok: false, status: 409, data: { detail: 'bind: Address already in use' } });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/deploy/tunnel/route');
    const response = (await POST(req({ action: 'start' }), undefined)) as Response;

    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ detail: 'bind: Address already in use' });
  });

  it('returns 502 with a structured error when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/deploy/tunnel/route');
    const response = (await POST(req({ action: 'start' }), undefined)) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: 'Failed to control the cloud access tunnel' });
  });

  it('returns 502 when the backend response body is not valid JSON', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.reject(new Error('invalid JSON')),
    });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/deploy/tunnel/route');
    const response = (await POST(req({ action: 'start' }), undefined)) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: 'Failed to control the cloud access tunnel' });
  });
});
