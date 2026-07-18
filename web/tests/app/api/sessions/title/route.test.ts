/**
 * Tests for the /api/sessions/[name]/title Next.js proxy route.
 *
 * Issue #3245: bring vitest coverage for API proxy routes to 100%. Like
 * sessions/init, this route has two independent try/catch blocks: invalid
 * JSON body -> 400, and backend fetch failure -> 503. Note this route does
 * NOT call decodeURIComponent on `name` before re-encoding it (unlike most
 * of the other dynamic-segment routes) — it uses the raw route param
 * directly in encodeURIComponent(name).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

describe('/api/sessions/[name]/title POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST body to backend session title endpoint', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      text: () => Promise.resolve(JSON.stringify({ title: 'New Title' })),
    });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/title/route'
    );
    const req = new Request('http://localhost/api/sessions/my-session/title', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: 'New Title' }),
    });
    const response = await POST(req, {
      params: Promise.resolve({ name: 'my-session' }),
    }) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/sessions/my-session/title');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ title: 'New Title' });
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ title: 'New Title' });
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      text: () => Promise.resolve('{}'),
    });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/title/route'
    );
    const req = new Request('http://localhost/api/sessions/test/title', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: 'x' }),
    });
    await POST(req, { params: Promise.resolve({ name: 'test' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/sessions/test/title');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      text: () => Promise.resolve('{}'),
    });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/title/route'
    );
    const req = new Request('http://localhost/api/sessions/test/title', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: 'x' }),
    });
    await POST(req, { params: Promise.resolve({ name: 'test' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/sessions/test/title');
  });

  it('URL-encodes the session name', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      text: () => Promise.resolve('{}'),
    });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/title/route'
    );
    const req = new Request(
      'http://localhost/api/sessions/my%20session/title',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: 'x' }),
      }
    );
    await POST(req, { params: Promise.resolve({ name: 'my%20session' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    // The route does NOT decodeURIComponent `name` first, so the already
    // percent-encoded route param gets double-encoded by encodeURIComponent.
    expect(calledUrl).toBe(
      'http://127.0.0.1:8000/api/v1/sessions/my%2520session/title'
    );
  });

  it('passes through backend response status', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 404,
      text: () => Promise.resolve(JSON.stringify({ error: 'not found' })),
    });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/title/route'
    );
    const req = new Request('http://localhost/api/sessions/missing/title', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: 'x' }),
    });
    const response = await POST(req, {
      params: Promise.resolve({ name: 'missing' }),
    }) as Response;

    expect(response.status).toBe(404);
  });

  it('returns 400 when request body is invalid JSON', async () => {
    global.fetch = vi.fn();

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/title/route'
    );
    const req = new Request('http://localhost/api/sessions/my-session/title', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: 'not valid json{{{',
    });
    const response = await POST(req, {
      params: Promise.resolve({ name: 'my-session' }),
    }) as Response;

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body).toEqual({ error: { message: 'Invalid JSON in request body' } });
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('returns 503 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/title/route'
    );
    const req = new Request('http://localhost/api/sessions/my-session/title', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: 'x' }),
    });
    const response = await POST(req, {
      params: Promise.resolve({ name: 'my-session' }),
    }) as Response;

    expect(response.status).toBe(503);
    const body = await response.json();
    expect(body).toEqual({ error: { message: 'Backend service unavailable' } });
  });
});
