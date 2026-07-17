/**
 * Tests for the /api/v1/rag/collections/[name]/settings Next.js proxy
 * route (issue #3245). Covers both the GET and PUT handlers.
 *
 * Both handlers await res.json() unconditionally (no .catch fallback)
 * before branching on res.ok, and both wrap everything in try/catch.
 * Covers: success, backend non-ok passthrough, a request-body JSON parse
 * failure on PUT, and a network failure (fetch throws) on both.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(response: { ok: boolean; status: number; json?: () => Promise<unknown> }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: response.ok,
    status: response.status,
    json: response.json ?? vi.fn().mockResolvedValue({}),
  });
}

describe('/api/v1/rag/collections/[name]/settings GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to the backend using the default URL with no-store caching', async () => {
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ chunk_size: 512 }) });

    const { GET } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/settings/route'
    );
    const response = (await GET(
      new Request('http://localhost/api/v1/rag/collections/my-collection/settings'),
      { params: Promise.resolve({ name: 'my-collection' }) }
    )) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe(
      'http://127.0.0.1:8000/api/v1/rag/collections/my-collection/settings'
    );
    expect(calledOptions.method).toBe('GET');
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ chunk_size: 512 });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({}) });

    const { GET } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/settings/route'
    );
    await GET(new Request('http://localhost/api/v1/rag/collections/test/settings'), {
      params: Promise.resolve({ name: 'test' }),
    });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/rag/collections/test/settings');
  });

  it('URL-encodes the collection name', async () => {
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({}) });

    const { GET } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/settings/route'
    );
    await GET(new Request('http://localhost/api/v1/rag/collections/my%20collection/settings'), {
      params: Promise.resolve({ name: 'my collection' }),
    });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toContain('my%20collection/settings');
  });

  it('passes through backend error JSON and status when the backend responds non-ok', async () => {
    mockFetch({
      ok: false,
      status: 404,
      json: vi.fn().mockResolvedValue({ detail: 'collection not found' }),
    });

    const { GET } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/settings/route'
    );
    const response = (await GET(
      new Request('http://localhost/api/v1/rag/collections/missing/settings'),
      { params: Promise.resolve({ name: 'missing' }) }
    )) as Response;

    expect(response.status).toBe(404);
    const body = await response.json();
    expect(body).toEqual({ detail: 'collection not found' });
  });

  it('returns 502 with structured error when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/settings/route'
    );
    const response = (await GET(
      new Request('http://localhost/api/v1/rag/collections/x/settings'),
      { params: Promise.resolve({ name: 'x' }) }
    )) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to fetch settings from backend' });
  });
});

describe('/api/v1/rag/collections/[name]/settings PUT route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  function putRequest(url: string, body: unknown) {
    return new Request(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  it('forwards PUT body to the backend using the default URL', async () => {
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ chunk_size: 1024 }) });

    const { PUT } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/settings/route'
    );
    const response = (await PUT(
      putRequest('http://localhost/api/v1/rag/collections/my-collection/settings', {
        chunk_size: 1024,
      }),
      { params: Promise.resolve({ name: 'my-collection' }) }
    )) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe(
      'http://127.0.0.1:8000/api/v1/rag/collections/my-collection/settings'
    );
    expect(calledOptions.method).toBe('PUT');
    expect(JSON.parse(calledOptions.body)).toEqual({ chunk_size: 1024 });

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ chunk_size: 1024 });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({}) });

    const { PUT } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/settings/route'
    );
    await PUT(putRequest('http://localhost/api/v1/rag/collections/test/settings', {}), {
      params: Promise.resolve({ name: 'test' }),
    });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/rag/collections/test/settings');
  });

  it('URL-encodes the collection name', async () => {
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({}) });

    const { PUT } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/settings/route'
    );
    await PUT(
      putRequest('http://localhost/api/v1/rag/collections/my%20collection/settings', {}),
      { params: Promise.resolve({ name: 'my collection' }) }
    );

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toContain('my%20collection/settings');
  });

  it('passes through backend error JSON and status when the backend responds non-ok', async () => {
    mockFetch({
      ok: false,
      status: 400,
      json: vi.fn().mockResolvedValue({ detail: 'invalid settings' }),
    });

    const { PUT } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/settings/route'
    );
    const response = (await PUT(
      putRequest('http://localhost/api/v1/rag/collections/my-collection/settings', {
        chunk_size: -1,
      }),
      { params: Promise.resolve({ name: 'my-collection' }) }
    )) as Response;

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body).toEqual({ detail: 'invalid settings' });
  });

  it('returns 502 when the request body is not valid JSON', async () => {
    global.fetch = vi.fn();
    const { PUT } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/settings/route'
    );
    const request = new Request(
      'http://localhost/api/v1/rag/collections/my-collection/settings',
      { method: 'PUT', body: 'not json' }
    );
    const response = (await PUT(request, {
      params: Promise.resolve({ name: 'my-collection' }),
    })) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to update settings' });
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('returns 502 with structured error when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { PUT } = await import(
      '../../../../../../../../src/app/api/v1/rag/collections/[name]/settings/route'
    );
    const response = (await PUT(
      putRequest('http://localhost/api/v1/rag/collections/x/settings', {}),
      { params: Promise.resolve({ name: 'x' }) }
    )) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to update settings' });
  });
});
