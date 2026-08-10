/**
 * Tests for the /api/v1/cloud/state/unlock Next.js proxy route (#3510).
 *
 * Force-unlock breaks the mutual exclusion that remote state exists to
 * provide, and the backend requires the specific lock id rather than
 * "release whatever is held". The proxy must forward that id untouched.
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
  return new Request('http://localhost/api/v1/cloud/state/unlock', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

describe('/api/v1/cloud/state/unlock proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards the lock id to backend using default URL', async () => {
    mockFetch({ ok: true, status: 200, data: { unlocked: true, lock_id: 'abc-123' } });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/state/unlock/route');
    const response = (await POST(req({ lock_id: 'abc-123' }), undefined)) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/cloud/state/unlock');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ lock_id: 'abc-123' });

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ unlocked: true, lock_id: 'abc-123' });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/state/unlock/route');
    await POST(req({ lock_id: 'abc-123' }), undefined);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/cloud/state/unlock');
  });

  it('falls back to an empty body when request JSON is invalid', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/state/unlock/route');
    const badReq = new Request('http://localhost/api/v1/cloud/state/unlock', {
      method: 'POST',
      body: 'not json',
    });
    await POST(badReq, undefined);

    const calledOptions = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(JSON.parse(calledOptions.body)).toEqual({});
  });

  it('passes through the backend refusal of a missing lock id', async () => {
    mockFetch({ ok: false, status: 400, data: { detail: 'A lock_id is required.' } });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/state/unlock/route');
    const response = (await POST(req(), undefined)) as Response;

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body).toEqual({ detail: 'A lock_id is required.' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/state/unlock/route');
    const response = (await POST(req({ lock_id: 'abc-123' }), undefined)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to release the Terraform state lock' });
  });

  it('returns 502 when the backend response body is not valid JSON', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.reject(new Error('invalid JSON')),
    });

    const { POST } = await import('../../../../../../../src/app/api/v1/cloud/state/unlock/route');
    const response = (await POST(req({ lock_id: 'abc-123' }), undefined)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to release the Terraform state lock' });
  });
});
