/**
 * Tests for the /api/v1/rag/collections/[name] Next.js proxy route (DELETE).
 *
 * Issue #3245: bring vitest coverage for API proxy routes to 100%.
 * The non-ok branch attempts res.json() and falls back to a synthetic
 * error body via `.catch()` when the backend response has no JSON body,
 * so both the "backend returned JSON" and "backend returned no body"
 * sub-branches need coverage.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(response: { ok: boolean; status: number; json?: () => Promise<unknown> }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: response.ok,
    status: response.status,
    json: response.json ?? vi.fn().mockResolvedValue({}),
    headers: new Headers({ 'Content-Type': 'application/json' }),
  });
}

describe('/api/v1/rag/collections/[name] DELETE route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards DELETE to backend collection endpoint', async () => {
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ deleted: true }) });

    const { DELETE } = await import(
      '../../../../../../../src/app/api/v1/rag/collections/[name]/route'
    );
    await DELETE(new Request('http://localhost/api/v1/rag/collections/my-collection'), {
      params: Promise.resolve({ name: 'my-collection' }),
    });

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/rag/collections/my-collection');
    expect(calledOptions.method).toBe('DELETE');
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({}) });

    const { DELETE } = await import(
      '../../../../../../../src/app/api/v1/rag/collections/[name]/route'
    );
    await DELETE(new Request('http://localhost/api/v1/rag/collections/test'), {
      params: Promise.resolve({ name: 'test' }),
    });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/rag/collections/test');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({}) });

    const { DELETE } = await import(
      '../../../../../../../src/app/api/v1/rag/collections/[name]/route'
    );
    await DELETE(new Request('http://localhost/api/v1/rag/collections/test'), {
      params: Promise.resolve({ name: 'test' }),
    });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/rag/collections/test');
  });

  it('URL-encodes the collection name', async () => {
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({}) });

    const { DELETE } = await import(
      '../../../../../../../src/app/api/v1/rag/collections/[name]/route'
    );
    await DELETE(new Request('http://localhost/api/v1/rag/collections/my%20collection'), {
      params: Promise.resolve({ name: 'my collection' }),
    });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toContain('my%20collection');
  });

  it('returns 200 with backend JSON on success', async () => {
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ deleted: true }) });

    const { DELETE } = await import(
      '../../../../../../../src/app/api/v1/rag/collections/[name]/route'
    );
    const response = await DELETE(
      new Request('http://localhost/api/v1/rag/collections/my-collection'),
      { params: Promise.resolve({ name: 'my-collection' }) }
    ) as Response;

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ deleted: true });
  });

  it('passes through backend error JSON when not ok', async () => {
    mockFetch({
      ok: false,
      status: 404,
      json: vi.fn().mockResolvedValue({ detail: 'collection not found' }),
    });

    const { DELETE } = await import(
      '../../../../../../../src/app/api/v1/rag/collections/[name]/route'
    );
    const response = await DELETE(
      new Request('http://localhost/api/v1/rag/collections/missing'),
      { params: Promise.resolve({ name: 'missing' }) }
    ) as Response;

    expect(response.status).toBe(404);
    const body = await response.json();
    expect(body).toEqual({ detail: 'collection not found' });
  });

  it('falls back to synthetic error body when backend error response has no JSON', async () => {
    mockFetch({
      ok: false,
      status: 500,
      json: vi.fn().mockRejectedValue(new Error('no body')),
    });

    const { DELETE } = await import(
      '../../../../../../../src/app/api/v1/rag/collections/[name]/route'
    );
    const response = await DELETE(
      new Request('http://localhost/api/v1/rag/collections/broken'),
      { params: Promise.resolve({ name: 'broken' }) }
    ) as Response;

    expect(response.status).toBe(500);
    const body = await response.json();
    expect(body).toEqual({ error: 'Backend returned 500' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { DELETE } = await import(
      '../../../../../../../src/app/api/v1/rag/collections/[name]/route'
    );
    const response = await DELETE(
      new Request('http://localhost/api/v1/rag/collections/x'),
      { params: Promise.resolve({ name: 'x' }) }
    ) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to delete collection from backend' });
  });
});
