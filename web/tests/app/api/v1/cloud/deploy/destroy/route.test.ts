/**
 * Tests for the /api/v1/cloud/deploy/destroy Next.js proxy route (#3513).
 *
 * This is the teardown path the dashboard uses instead of the raw substrate
 * destroy, so that the access tunnel is closed before the instance it points
 * at disappears. The confirmation the caller sends must reach the backend
 * verbatim -- a dropped `yes` is the difference between a refusal and a
 * deleted instance -- and a backend refusal must never read like success.
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
  return new Request('http://localhost/api/v1/cloud/deploy/destroy', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

describe('/api/v1/cloud/deploy/destroy proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards the teardown request and its confirmation to the backend', async () => {
    mockFetch({ ok: true, status: 200, data: { action: 'destroy', tunnel_stopped: true } });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/cloud/deploy/destroy/route'
    );
    const response = (await POST(req({ confirm: 'yes' }), undefined)) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/cloud/deploy/destroy');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ confirm: 'yes' });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ action: 'destroy', tunnel_stopped: true });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/cloud/deploy/destroy/route'
    );
    await POST(req(), undefined);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/cloud/deploy/destroy');
  });

  it('falls back to an empty body when the request JSON is invalid', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/cloud/deploy/destroy/route'
    );
    const badReq = new Request('http://localhost/api/v1/cloud/deploy/destroy', {
      method: 'POST',
      body: 'not json',
    });
    await POST(badReq, undefined);

    const calledOptions = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(JSON.parse(calledOptions.body)).toEqual({});
  });

  it('passes through an unconfirmed teardown refusal instead of reporting success', async () => {
    mockFetch({ ok: false, status: 400, data: { error: 'destroy requires confirmation' } });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/cloud/deploy/destroy/route'
    );
    const response = (await POST(req(), undefined)) as Response;

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: 'destroy requires confirmation' });
  });

  it('returns 502 with a structured error when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/cloud/deploy/destroy/route'
    );
    const response = (await POST(req({ confirm: 'yes' }), undefined)) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: 'Failed to destroy the cloud deployment' });
  });

  it('returns 502 when the backend response body is not valid JSON', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.reject(new Error('invalid JSON')),
    });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/cloud/deploy/destroy/route'
    );
    const response = (await POST(req({ confirm: 'yes' }), undefined)) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: 'Failed to destroy the cloud deployment' });
  });
});
