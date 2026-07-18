/**
 * Tests for the /api/sessions/[name]/messages/[index]/rag Next.js proxy
 * route.
 *
 * Issue #3245: bring vitest coverage for API proxy routes to 100%. No
 * try/catch — GET decodes the `name` and `index` dynamic segments (`index`
 * is decoded but NOT re-encoded when composing the backend URL, unlike
 * `name`).
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

describe('/api/sessions/[name]/messages/[index]/rag GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to backend message rag endpoint', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import(
      '../../../../../src/app/api/sessions/[name]/messages/[index]/rag/route'
    );
    await GET(
      new Request('http://localhost/api/sessions/my-session/messages/3/rag'),
      { params: Promise.resolve({ name: 'my-session', index: '3' }) }
    );

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/sessions/my-session/messages/3/rag');
    expect(calledOptions.cache).toBe('no-store');
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import(
      '../../../../../src/app/api/sessions/[name]/messages/[index]/rag/route'
    );
    await GET(
      new Request('http://localhost/api/sessions/test/messages/0/rag'),
      { params: Promise.resolve({ name: 'test', index: '0' }) }
    );

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/sessions/test/messages/0/rag');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import(
      '../../../../../src/app/api/sessions/[name]/messages/[index]/rag/route'
    );
    await GET(
      new Request('http://localhost/api/sessions/test/messages/0/rag'),
      { params: Promise.resolve({ name: 'test', index: '0' }) }
    );

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/sessions/test/messages/0/rag');
  });

  it('URL-encodes the session name but leaves the decoded index as-is', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import(
      '../../../../../src/app/api/sessions/[name]/messages/[index]/rag/route'
    );
    await GET(
      new Request('http://localhost/api/sessions/my%20session/messages/2/rag'),
      { params: Promise.resolve({ name: 'my%20session', index: '2' }) }
    );

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe(
      'http://127.0.0.1:8000/api/v1/sessions/my%20session/messages/2/rag'
    );
  });

  it('passes through backend response status', async () => {
    mockFetch({ ok: false, status: 404 });

    const { GET } = await import(
      '../../../../../src/app/api/sessions/[name]/messages/[index]/rag/route'
    );
    const response = await GET(
      new Request('http://localhost/api/sessions/s/messages/99/rag'),
      { params: Promise.resolve({ name: 's', index: '99' }) }
    ) as Response;

    expect(response.status).toBe(404);
  });
});
