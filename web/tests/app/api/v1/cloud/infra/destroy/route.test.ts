/**
 * Tests for the /api/v1/cloud/infra/destroy Next.js proxy route (#3509).
 *
 * Teardown is irreversible, so the route must forward the explicit
 * `{"confirm": true}` the backend requires rather than synthesising it, and a
 * backend that refuses an unconfirmed request must reach the caller unchanged.
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
  return new Request('http://localhost/api/v1/cloud/infra/destroy', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

describe('/api/v1/cloud/infra/destroy proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards the explicit confirmation to backend using default URL', async () => {
    mockFetch({ ok: true, status: 200, data: { action: 'destroy', settings: {} } });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/infra/destroy/route');
    const response = (await POST(req({ confirm: true }), undefined)) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/cloud/infra/destroy');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ confirm: true });

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ action: 'destroy', settings: {} });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/infra/destroy/route');
    await POST(req({ confirm: true }), undefined);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/cloud/infra/destroy');
  });

  it('does not invent a confirmation when the request JSON is invalid', async () => {
    mockFetch({ ok: false, status: 400, data: { error: 'destroy requires confirm=true' } });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/infra/destroy/route');
    const badReq = new Request('http://localhost/api/v1/cloud/infra/destroy', {
      method: 'POST',
      body: 'not json',
    });
    const response = (await POST(badReq, undefined)) as Response;

    const calledOptions = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(JSON.parse(calledOptions.body)).toEqual({});
    expect(response.status).toBe(400);
  });

  it('passes through the backend refusal of an unconfirmed teardown', async () => {
    mockFetch({ ok: false, status: 400, data: { error: 'destroy requires confirm=true' } });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/infra/destroy/route');
    const response = (await POST(req(), undefined)) as Response;

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body).toEqual({ error: 'destroy requires confirm=true' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/infra/destroy/route');
    const response = (await POST(req({ confirm: true }), undefined)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to destroy the AWS substrate' });
  });

  it('returns 502 when the backend response body is not valid JSON', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.reject(new Error('invalid JSON')),
    });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/infra/destroy/route');
    const response = (await POST(req({ confirm: true }), undefined)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to destroy the AWS substrate' });
  });
});
