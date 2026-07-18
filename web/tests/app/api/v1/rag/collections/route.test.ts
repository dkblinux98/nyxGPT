/**
 * Tests for the /api/v1/rag/collections Next.js proxy route.
 *
 * Issue #3245: bring vitest coverage for API proxy routes to 100%.
 * GET checks res.ok before parsing JSON and returns a synthetic error body
 * on non-2xx; POST always parses JSON first, then passes status through.
 * Both wrap the fetch call in try/catch and return a 502 fallback on
 * network failure.
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

describe('/api/v1/rag/collections GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to backend collections endpoint', async () => {
    mockFetch({ ok: true, status: 200, json: { collections: ['a'] } });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/rag/collections/route'
    );
    await GET();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/rag/collections');
    expect(calledOptions.method).toBe('GET');
    expect(calledOptions.cache).toBe('no-store');
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200, json: { collections: [] } });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/rag/collections/route'
    );
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/rag/collections');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, json: { collections: [] } });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/rag/collections/route'
    );
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/rag/collections');
  });

  it('returns 200 with backend JSON on success', async () => {
    mockFetch({ ok: true, status: 200, json: { collections: ['docs'] } });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/rag/collections/route'
    );
    const response = await GET() as Response;

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ collections: ['docs'] });
  });

  it('returns backend status with synthetic error body when not ok', async () => {
    mockFetch({ ok: false, status: 503 });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/rag/collections/route'
    );
    const response = await GET() as Response;

    expect(response.status).toBe(503);
    const body = await response.json();
    expect(body).toEqual({ error: 'Backend returned 503' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import(
      '../../../../../../src/app/api/v1/rag/collections/route'
    );
    const response = await GET() as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to fetch collections from backend' });
  });
});

describe('/api/v1/rag/collections POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST body to backend collections endpoint', async () => {
    mockFetch({ ok: true, status: 201, json: { name: 'new-collection' } });

    const { POST } = await import(
      '../../../../../../src/app/api/v1/rag/collections/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'new-collection' }),
    });
    await POST(req);

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/rag/collections');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ name: 'new-collection' });
  });

  it('uses NYXGPT_API_BASE_URL when set on POST', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 201, json: { name: 'x' } });

    const { POST } = await import(
      '../../../../../../src/app/api/v1/rag/collections/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'x' }),
    });
    await POST(req);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/rag/collections');
  });

  it('returns backend status and JSON on success', async () => {
    mockFetch({ ok: true, status: 201, json: { name: 'created' } });

    const { POST } = await import(
      '../../../../../../src/app/api/v1/rag/collections/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'created' }),
    });
    const response = await POST(req) as Response;

    expect(response.status).toBe(201);
    const body = await response.json();
    expect(body).toEqual({ name: 'created' });
  });

  it('passes through backend error status and JSON when not ok', async () => {
    mockFetch({ ok: false, status: 409, json: { detail: 'already exists' } });

    const { POST } = await import(
      '../../../../../../src/app/api/v1/rag/collections/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'dup' }),
    });
    const response = await POST(req) as Response;

    expect(response.status).toBe(409);
    const body = await response.json();
    expect(body).toEqual({ detail: 'already exists' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import(
      '../../../../../../src/app/api/v1/rag/collections/route'
    );
    const req = new Request('http://localhost/api/v1/rag/collections', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'x' }),
    });
    const response = await POST(req) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to create collection' });
  });
});
