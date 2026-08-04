/**
 * Tests for the /api/v1/config/secrets/sync Next.js proxy route (#3505).
 *
 * web/src/app/api/v1/config/secrets/sync/route.ts forwards POST to the
 * backend /api/v1/config/secrets/sync endpoint via apiFetch, passing r.body
 * straight through.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(response: { ok: boolean; status: number }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: response.ok,
    status: response.status,
    body: null,
    headers: new Headers({ 'Content-Type': 'application/json' }),
  });
}

describe('/api/v1/config/secrets/sync POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST body and method to backend', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../../../../src/app/api/v1/config/secrets/sync/route');
    const req = new Request('http://localhost/api/v1/config/secrets/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dry_run: true }),
    });
    await POST(req);

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/config/secrets/sync');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ dry_run: true });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../../../../src/app/api/v1/config/secrets/sync/route');
    const req = new Request('http://localhost/api/v1/config/secrets/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dry_run: false }),
    });
    await POST(req);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/config/secrets/sync');
  });

  it('falls back to an empty body when request JSON is invalid', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../../../../src/app/api/v1/config/secrets/sync/route');
    const req = new Request('http://localhost/api/v1/config/secrets/sync', {
      method: 'POST',
      body: 'not json',
    });
    await POST(req);

    const [, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(calledOptions.body)).toEqual({});
  });

  it('passes through a non-2xx backend response status', async () => {
    mockFetch({ ok: false, status: 502 });

    const { POST } = await import('../../../../../../../src/app/api/v1/config/secrets/sync/route');
    const req = new Request('http://localhost/api/v1/config/secrets/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dry_run: false }),
    });
    const response = (await POST(req)) as Response;

    expect(response.status).toBe(502);
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import('../../../../../../../src/app/api/v1/config/secrets/sync/route');
    const req = new Request('http://localhost/api/v1/config/secrets/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dry_run: true }),
    });
    const response = (await POST(req)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'config backend unavailable' });
  });
});
