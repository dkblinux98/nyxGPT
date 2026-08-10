/**
 * Tests for the /api/v1/cloud/state/restore Next.js proxy route (#3510).
 *
 * Restore rewrites what Terraform believes exists in AWS, and the backend
 * refuses it without an explicit `confirm`. The proxy must forward the body
 * verbatim -- injecting or dropping `confirm` here would either nullify that
 * guard or make an operator's confirmed restore fail.
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
  return new Request('http://localhost/api/v1/cloud/state/restore', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

describe('/api/v1/cloud/state/restore proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards the version id and confirmation to backend using default URL', async () => {
    mockFetch({ ok: true, status: 200, data: { restored: true, version_id: 'v1' } });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/cloud/state/restore/route'
    );
    const response = (await POST(
      req({ version_id: 'v1', confirm: true }),
      undefined
    )) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/cloud/state/restore');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ version_id: 'v1', confirm: true });

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ restored: true, version_id: 'v1' });
  });

  it('does not invent a confirmation the caller did not send', async () => {
    mockFetch({ ok: false, status: 400, data: { detail: 'Re-send with {"confirm": true}' } });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/cloud/state/restore/route'
    );
    const response = (await POST(req({ version_id: 'v1' }), undefined)) as Response;

    const calledOptions = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(JSON.parse(calledOptions.body)).toEqual({ version_id: 'v1' });
    expect(response.status).toBe(400);
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/cloud/state/restore/route'
    );
    await POST(req({ version_id: 'v1', confirm: true }), undefined);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/cloud/state/restore');
  });

  it('falls back to an empty body when request JSON is invalid', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/cloud/state/restore/route'
    );
    const badReq = new Request('http://localhost/api/v1/cloud/state/restore', {
      method: 'POST',
      body: 'not json',
    });
    await POST(badReq, undefined);

    const calledOptions = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(JSON.parse(calledOptions.body)).toEqual({});
  });

  it('passes through backend response status and body when restore is refused', async () => {
    mockFetch({ ok: false, status: 409, data: { detail: 'terraform state push failed' } });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/cloud/state/restore/route'
    );
    const response = (await POST(req({ version_id: 'v1', confirm: true }), undefined)) as Response;

    expect(response.status).toBe(409);
    const body = await response.json();
    expect(body).toEqual({ detail: 'terraform state push failed' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/cloud/state/restore/route'
    );
    const response = (await POST(req(), undefined)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to restore a Terraform state version' });
  });

  it('returns 502 when the backend response body is not valid JSON', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.reject(new Error('invalid JSON')),
    });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/cloud/state/restore/route'
    );
    const response = (await POST(req(), undefined)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to restore a Terraform state version' });
  });
});
