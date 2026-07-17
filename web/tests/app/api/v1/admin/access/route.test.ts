/**
 * Tests for the /api/v1/admin/access Next.js proxy route (issue #3245).
 *
 * web/src/app/api/v1/admin/access/route.ts forwards GET/POST to the backend
 * /api/v1/admin/access endpoint via apiFetch, passing r.body straight through.
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

describe('/api/v1/admin/access GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/admin/access/route');
    await GET();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/admin/access');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/admin/access/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/admin/access');
  });

  it('passes through a successful backend response status', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/admin/access/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(200);
  });

  it('passes through a non-2xx backend response status', async () => {
    mockFetch({ ok: false, status: 403 });

    const { GET } = await import('../../../../../../src/app/api/v1/admin/access/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(403);
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import('../../../../../../src/app/api/v1/admin/access/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'admin access backend unavailable' });
  });
});

describe('/api/v1/admin/access POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST body and method to backend', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../../../src/app/api/v1/admin/access/route');
    const req = new Request('http://localhost/api/v1/admin/access', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user: 'alice', role: 'admin' }),
    });
    await POST(req);

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/admin/access');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ user: 'alice', role: 'admin' });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../../../src/app/api/v1/admin/access/route');
    const req = new Request('http://localhost/api/v1/admin/access', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user: 'bob' }),
    });
    await POST(req);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/admin/access');
  });

  it('passes through a successful backend response status', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../../../src/app/api/v1/admin/access/route');
    const req = new Request('http://localhost/api/v1/admin/access', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user: 'carol' }),
    });
    const response = (await POST(req)) as Response;

    expect(response.status).toBe(200);
  });

  it('passes through a non-2xx backend response status', async () => {
    mockFetch({ ok: false, status: 422 });

    const { POST } = await import('../../../../../../src/app/api/v1/admin/access/route');
    const req = new Request('http://localhost/api/v1/admin/access', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user: 'dave' }),
    });
    const response = (await POST(req)) as Response;

    expect(response.status).toBe(422);
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import('../../../../../../src/app/api/v1/admin/access/route');
    const req = new Request('http://localhost/api/v1/admin/access', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user: 'erin' }),
    });
    const response = (await POST(req)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'admin access backend unavailable' });
  });
});
