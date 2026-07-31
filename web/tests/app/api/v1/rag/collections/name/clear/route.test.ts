/**
 * Tests for the /api/v1/rag/collections/[name]/clear Next.js proxy route (POST).
 *
 * Issue #3489: split "Clear Collection" (truncate, keep collection/settings)
 * from "Delete Collection" (drop table + settings) and route each through
 * its own proxy endpoint. This covers the new clear route: success
 * passthrough, backend non-ok passthrough, and the fetch-throws -> 502 branch.
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

describe('/api/v1/rag/collections/[name]/clear POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST to the backend clear endpoint', async () => {
    mockFetch({ ok: true, status: 200, json: { doc_count: 0, chunk_count: 0 } });

    const { POST } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/clear/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections/my-collection/clear', {
      method: 'POST',
    });
    await POST(req, { params: Promise.resolve({ name: 'my-collection' }) });

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/rag/collections/my-collection/clear');
    expect(calledOptions.method).toBe('POST');
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200, json: {} });

    const { POST } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/clear/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections/test/clear', {
      method: 'POST',
    });
    await POST(req, { params: Promise.resolve({ name: 'test' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/rag/collections/test/clear');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, json: {} });

    const { POST } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/clear/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections/test/clear', {
      method: 'POST',
    });
    await POST(req, { params: Promise.resolve({ name: 'test' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/rag/collections/test/clear');
  });

  it('URL-encodes the collection name', async () => {
    mockFetch({ ok: true, status: 200, json: {} });

    const { POST } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/clear/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections/my%20collection/clear', {
      method: 'POST',
    });
    await POST(req, { params: Promise.resolve({ name: 'my collection' }) });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toContain('my%20collection/clear');
  });

  it('returns backend status and JSON on success', async () => {
    mockFetch({ ok: true, status: 200, json: { doc_count: 0, chunk_count: 0 } });

    const { POST } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/clear/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections/my-collection/clear', {
      method: 'POST',
    });
    const response = (await POST(req, {
      params: Promise.resolve({ name: 'my-collection' }),
    })) as Response;

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ doc_count: 0, chunk_count: 0 });
  });

  it('passes through backend error status and JSON when not ok', async () => {
    mockFetch({ ok: false, status: 404, json: { detail: 'collection not found' } });

    const { POST } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/clear/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections/missing/clear', {
      method: 'POST',
    });
    const response = (await POST(req, {
      params: Promise.resolve({ name: 'missing' }),
    })) as Response;

    expect(response.status).toBe(404);
    const body = await response.json();
    expect(body).toEqual({ detail: 'collection not found' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/clear/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections/x/clear', {
      method: 'POST',
    });
    const response = (await POST(req, { params: Promise.resolve({ name: 'x' }) })) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to clear collection from backend' });
  });
});
