/**
 * Tests for the /api/sessions/[name]/rag/[action] Next.js proxy route.
 *
 * Issue #3245: bring vitest coverage for API proxy routes to 100%. No
 * try/catch, but the handler validates `action` against an allow-list
 * ('enable' | 'disable') and returns 400 for anything else before ever
 * calling the backend.
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

describe('/api/sessions/[name]/rag/[action] POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('returns 400 for an invalid action without calling the backend', async () => {
    global.fetch = vi.fn();
    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/rag/[action]/route'
    );
    const response = await POST(
      new Request('http://localhost/api/sessions/my-session/rag/toggle'),
      { params: Promise.resolve({ name: 'my-session', action: 'toggle' }) }
    ) as Response;

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body).toEqual({ error: 'Invalid action' });
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('forwards POST to backend for the "enable" action', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/rag/[action]/route'
    );
    await POST(
      new Request('http://localhost/api/sessions/my-session/rag/enable'),
      { params: Promise.resolve({ name: 'my-session', action: 'enable' }) }
    );

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/sessions/my-session/rag/enable');
    expect(calledOptions.method).toBe('POST');
  });

  it('forwards POST to backend for the "disable" action', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/rag/[action]/route'
    );
    await POST(
      new Request('http://localhost/api/sessions/my-session/rag/disable'),
      { params: Promise.resolve({ name: 'my-session', action: 'disable' }) }
    );

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toContain('/api/v1/sessions/my-session/rag/disable');
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/rag/[action]/route'
    );
    await POST(
      new Request('http://localhost/api/sessions/test/rag/enable'),
      { params: Promise.resolve({ name: 'test', action: 'enable' }) }
    );

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/sessions/test/rag/enable');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/rag/[action]/route'
    );
    await POST(
      new Request('http://localhost/api/sessions/test/rag/enable'),
      { params: Promise.resolve({ name: 'test', action: 'enable' }) }
    );

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/sessions/test/rag/enable');
  });

  it('decodes then URL-encodes the session name', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/rag/[action]/route'
    );
    await POST(
      new Request('http://localhost/api/sessions/my%20session/rag/enable'),
      { params: Promise.resolve({ name: 'my%20session', action: 'enable' }) }
    );

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe(
      'http://127.0.0.1:8000/api/v1/sessions/my%20session/rag/enable'
    );
  });

  it('passes through backend response status', async () => {
    mockFetch({ ok: false, status: 404 });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/rag/[action]/route'
    );
    const response = await POST(
      new Request('http://localhost/api/sessions/missing/rag/enable'),
      { params: Promise.resolve({ name: 'missing', action: 'enable' }) }
    ) as Response;

    expect(response.status).toBe(404);
  });
});
