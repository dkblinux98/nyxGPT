/**
 * Tests for the /api/sessions/init Next.js proxy route.
 *
 * Issue #3245: bring vitest coverage for API proxy routes to 100%. This
 * route has two independent try/catch blocks:
 *   1. Parsing the request body as JSON -> 400 on invalid JSON.
 *   2. Forwarding to the backend -> 503 with a structured error if the
 *      backend fetch throws (e.g. connection refused).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

describe('/api/sessions/init POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST body to backend sessions/init endpoint', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      text: () => Promise.resolve(JSON.stringify({ session: 'new-session' })),
    });

    const { POST } = await import('../../../../../src/app/api/sessions/init/route');
    const req = new Request('http://localhost/api/sessions/init', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'new-session' }),
    });
    const response = await POST(req) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/sessions/init');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ name: 'new-session' });
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ session: 'new-session' });
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      text: () => Promise.resolve('{}'),
    });

    const { POST } = await import('../../../../../src/app/api/sessions/init/route');
    const req = new Request('http://localhost/api/sessions/init', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    await POST(req);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/sessions/init');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      text: () => Promise.resolve('{}'),
    });

    const { POST } = await import('../../../../../src/app/api/sessions/init/route');
    const req = new Request('http://localhost/api/sessions/init', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    await POST(req);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/sessions/init');
  });

  it('passes through backend response status', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 409,
      text: () => Promise.resolve(JSON.stringify({ error: 'already exists' })),
    });

    const { POST } = await import('../../../../../src/app/api/sessions/init/route');
    const req = new Request('http://localhost/api/sessions/init', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'dup' }),
    });
    const response = await POST(req) as Response;

    expect(response.status).toBe(409);
  });

  it('returns 400 when request body is invalid JSON', async () => {
    global.fetch = vi.fn();

    const { POST } = await import('../../../../../src/app/api/sessions/init/route');
    const req = new Request('http://localhost/api/sessions/init', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: 'not valid json{{{',
    });
    const response = await POST(req) as Response;

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body).toEqual({ error: { message: 'Invalid JSON in request body' } });
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('returns 503 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import('../../../../../src/app/api/sessions/init/route');
    const req = new Request('http://localhost/api/sessions/init', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'x' }),
    });
    const response = await POST(req) as Response;

    expect(response.status).toBe(503);
    const body = await response.json();
    expect(body).toEqual({ error: { message: 'Backend service unavailable' } });
  });
});
