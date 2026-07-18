/**
 * Tests for the /api/models Next.js proxy route.
 *
 * Issue #3245: bring vitest coverage for API proxy routes to 100%. Exercises
 * GET (list models), POST (pull model) and DELETE (remove model, including
 * the missing-`model`-query-param validation branch).
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

describe('/api/models GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to backend models endpoint', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../src/app/api/models/route');
    await GET();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/models');
    expect(calledOptions.cache).toBe('no-store');
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../src/app/api/models/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/models');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../src/app/api/models/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/models');
  });

  it('passes through backend response status', async () => {
    mockFetch({ ok: false, status: 502 });

    const { GET } = await import('../../../../src/app/api/models/route');
    const response = await GET() as Response;

    expect(response.status).toBe(502);
  });
});

describe('/api/models POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST body to backend models/pull endpoint', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../src/app/api/models/route');
    const req = new Request('http://localhost/api/models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: 'llama3' }),
    });
    await POST(req);

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/models/pull');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ model: 'llama3' });
  });

  it('passes through backend response status on POST', async () => {
    mockFetch({ ok: false, status: 400 });

    const { POST } = await import('../../../../src/app/api/models/route');
    const req = new Request('http://localhost/api/models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: 'bad-model' }),
    });
    const response = await POST(req) as Response;

    expect(response.status).toBe(400);
  });
});

describe('/api/models DELETE route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('returns 400 when the model query parameter is missing', async () => {
    const { DELETE } = await import('../../../../src/app/api/models/route');
    const req = new Request('http://localhost/api/models');
    const response = await DELETE(req) as Response;

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body).toEqual({ error: 'Missing model parameter' });
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('forwards DELETE with URL-encoded model name to backend', async () => {
    mockFetch({ ok: true, status: 200 });

    const { DELETE } = await import('../../../../src/app/api/models/route');
    const req = new Request('http://localhost/api/models?model=llama%203');
    await DELETE(req);

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/models/llama%203');
    expect(calledOptions.method).toBe('DELETE');
  });

  it('passes through backend response status on DELETE', async () => {
    mockFetch({ ok: false, status: 404 });

    const { DELETE } = await import('../../../../src/app/api/models/route');
    const req = new Request('http://localhost/api/models?model=missing-model');
    const response = await DELETE(req) as Response;

    expect(response.status).toBe(404);
  });
});
