/**
 * Tests for the /api/sessions/[name]/delete Next.js proxy route.
 *
 * Issue #3245: bring vitest coverage for API proxy routes to 100%. No
 * try/catch here — POST decodes the dynamic `name` segment, re-encodes it,
 * and forwards a DELETE to the backend session resource.
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

describe('/api/sessions/[name]/delete POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards a DELETE to the backend session endpoint', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/delete/route'
    );
    await POST(new Request('http://localhost/api/sessions/my-session/delete'), {
      params: Promise.resolve({ name: 'my-session' }),
    });

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/sessions/my-session');
    expect(calledOptions.method).toBe('DELETE');
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/delete/route'
    );
    await POST(new Request('http://localhost/api/sessions/test/delete'), {
      params: Promise.resolve({ name: 'test' }),
    });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/sessions/test');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/delete/route'
    );
    await POST(new Request('http://localhost/api/sessions/test/delete'), {
      params: Promise.resolve({ name: 'test' }),
    });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/sessions/test');
  });

  it('decodes then URL-encodes the session name', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/delete/route'
    );
    await POST(
      new Request('http://localhost/api/sessions/my%20session/delete'),
      { params: Promise.resolve({ name: 'my%20session' }) }
    );

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/sessions/my%20session');
  });

  it('passes through backend response status', async () => {
    mockFetch({ ok: false, status: 404 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/delete/route'
    );
    const response = await POST(
      new Request('http://localhost/api/sessions/missing/delete'),
      { params: Promise.resolve({ name: 'missing' }) }
    ) as Response;

    expect(response.status).toBe(404);
  });
});
