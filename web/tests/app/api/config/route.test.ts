/**
 * Tests for the /api/config Next.js proxy route.
 *
 * Issue #3245: bring vitest coverage for API proxy routes to 100%. This
 * route has no try/catch — GET and POST both just forward to the backend
 * and pass through status/body.
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

describe('/api/config GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to backend config endpoint', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../src/app/api/config/route');
    await GET();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/config');
    expect(calledOptions.cache).toBe('no-store');
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../src/app/api/config/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/config');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../src/app/api/config/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/config');
  });

  it('passes through backend response status', async () => {
    mockFetch({ ok: false, status: 503 });

    const { GET } = await import('../../../../src/app/api/config/route');
    const response = await GET() as Response;

    expect(response.status).toBe(503);
    expect(response.headers.get('Content-Type')).toBe('application/json');
  });
});

describe('/api/config POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST body to backend config endpoint', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../src/app/api/config/route');
    const req = new Request('http://localhost/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: 'dark' }),
    });
    await POST(req);

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/config');
    expect(calledOptions.method).toBe('POST');
    expect(calledOptions.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(calledOptions.body)).toEqual({ theme: 'dark' });
  });

  it('uses NYXGPT_API_BASE_URL when set on POST', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../src/app/api/config/route');
    const req = new Request('http://localhost/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: 'light' }),
    });
    await POST(req);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/config');
  });

  it('passes through backend response status on POST', async () => {
    mockFetch({ ok: false, status: 422 });

    const { POST } = await import('../../../../src/app/api/config/route');
    const req = new Request('http://localhost/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: 'bad' }),
    });
    const response = await POST(req) as Response;

    expect(response.status).toBe(422);
  });
});
