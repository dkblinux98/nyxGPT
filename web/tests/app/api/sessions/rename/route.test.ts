/**
 * Tests for the /api/sessions/[name]/rename Next.js proxy route.
 *
 * Issue #3245: bring vitest coverage for API proxy routes to 100%. No
 * try/catch — POST reads a JSON body and forwards it verbatim, decoding
 * then re-encoding the `name` dynamic segment.
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

describe('/api/sessions/[name]/rename POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST body to backend session rename endpoint', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/rename/route'
    );
    const req = new Request('http://localhost/api/sessions/my-session/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_name: 'renamed-session' }),
    });
    await POST(req, { params: Promise.resolve({ name: 'my-session' }) });

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/sessions/my-session/rename');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ new_name: 'renamed-session' });
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/rename/route'
    );
    const req = new Request('http://localhost/api/sessions/test/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_name: 'x' }),
    });
    await POST(req, { params: Promise.resolve({ name: 'test' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/sessions/test/rename');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/rename/route'
    );
    const req = new Request('http://localhost/api/sessions/test/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_name: 'x' }),
    });
    await POST(req, { params: Promise.resolve({ name: 'test' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/sessions/test/rename');
  });

  it('decodes then URL-encodes the session name', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/rename/route'
    );
    const req = new Request(
      'http://localhost/api/sessions/my%20session/rename',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_name: 'x' }),
      }
    );
    await POST(req, { params: Promise.resolve({ name: 'my%20session' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/sessions/my%20session/rename');
  });

  it('passes through backend response status', async () => {
    mockFetch({ ok: false, status: 409 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/rename/route'
    );
    const req = new Request('http://localhost/api/sessions/dup/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_name: 'existing-name' }),
    });
    const response = await POST(req, {
      params: Promise.resolve({ name: 'dup' }),
    }) as Response;

    expect(response.status).toBe(409);
  });
});
