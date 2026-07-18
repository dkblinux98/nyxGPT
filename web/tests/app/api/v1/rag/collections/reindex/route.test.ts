/**
 * Tests for the /api/v1/rag/collections/[name]/reindex Next.js proxy route (POST).
 *
 * Issue #3245: bring vitest coverage for API proxy routes to 100%.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(response: { ok: boolean; status: number; json?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: response.ok,
    status: response.status,
    json: vi.fn().mockResolvedValue(response.json ?? {}),
    headers: new Headers({ 'Content-Type': 'application/json' }),
  });
}

describe('/api/v1/rag/collections/[name]/reindex POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST body to backend reindex endpoint', async () => {
    mockFetch({ ok: true, status: 200, json: { status: 'started' } });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/rag/collections/[name]/reindex/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections/my-collection/reindex', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force: true }),
    });
    await POST(req, { params: Promise.resolve({ name: 'my-collection' }) });

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/rag/collections/my-collection/reindex');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ force: true });
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200, json: {} });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/rag/collections/[name]/reindex/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections/test/reindex', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    await POST(req, { params: Promise.resolve({ name: 'test' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/rag/collections/test/reindex');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, json: {} });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/rag/collections/[name]/reindex/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections/test/reindex', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    await POST(req, { params: Promise.resolve({ name: 'test' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/rag/collections/test/reindex');
  });

  it('URL-encodes the collection name', async () => {
    mockFetch({ ok: true, status: 200, json: {} });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/rag/collections/[name]/reindex/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections/my%20collection/reindex', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    await POST(req, { params: Promise.resolve({ name: 'my collection' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toContain('my%20collection/reindex');
  });

  it('returns backend status and JSON on success', async () => {
    mockFetch({ ok: true, status: 200, json: { status: 'started' } });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/rag/collections/[name]/reindex/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections/my-collection/reindex', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const response = await POST(req, { params: Promise.resolve({ name: 'my-collection' }) }) as Response;

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ status: 'started' });
  });

  it('passes through backend error status and JSON when not ok', async () => {
    mockFetch({ ok: false, status: 404, json: { detail: 'collection not found' } });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/rag/collections/[name]/reindex/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections/missing/reindex', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const response = await POST(req, { params: Promise.resolve({ name: 'missing' }) }) as Response;

    expect(response.status).toBe(404);
    const body = await response.json();
    expect(body).toEqual({ detail: 'collection not found' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/rag/collections/[name]/reindex/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections/x/reindex', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const response = await POST(req, { params: Promise.resolve({ name: 'x' }) }) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to re-index collection' });
  });
});
