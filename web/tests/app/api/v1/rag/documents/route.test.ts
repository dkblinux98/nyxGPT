/**
 * Tests for the /api/v1/rag/documents Next.js proxy route (issue #3245).
 *
 * The handler wraps the backend call in try/catch, parses the backend body
 * with res.json() (no .catch fallback), and branches on res.ok before
 * deciding the response shape. Covers: success, non-ok backend status
 * (short-circuits before res.json()), fetch throwing (network failure),
 * and res.json() rejecting on the success path (backend returned bad JSON).
 *
 * Also covers forwarding the incoming request's query string (`collection`,
 * etc.) to the backend -- previously dropped entirely, which meant the
 * chat RAG "Select Documents" panel always listed the `default` collection's
 * documents no matter which collection was selected in the UI (#3583).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(response: { ok: boolean; status: number; json?: () => Promise<unknown> }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: response.ok,
    status: response.status,
    json: response.json ?? vi.fn().mockResolvedValue({}),
  });
}

function makeRequest(url: string): Request {
  return new Request(url);
}

describe('/api/v1/rag/documents GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to the backend using the default URL with no-store caching', async () => {
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ documents: [] }) });

    const { GET } = await import('../../../../../../src/app/api/v1/rag/documents/route');
    const response = (await GET(makeRequest('http://localhost/api/v1/rag/documents'))) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/rag/documents');
    expect(calledOptions.method).toBe('GET');
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ documents: [] });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ documents: [] }) });

    const { GET } = await import('../../../../../../src/app/api/v1/rag/documents/route');
    await GET(makeRequest('http://localhost/api/v1/rag/documents'));

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/rag/documents');
  });

  it('forwards the collection query parameter to the backend', async () => {
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ documents: [] }) });

    const { GET } = await import('../../../../../../src/app/api/v1/rag/documents/route');
    await GET(makeRequest('http://localhost/api/v1/rag/documents?collection=test_collection'));

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/rag/documents?collection=test_collection');
  });

  it('omits the collection query param when none is supplied (backend defaults to "default")', async () => {
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ documents: [] }) });

    const { GET } = await import('../../../../../../src/app/api/v1/rag/documents/route');
    await GET(makeRequest('http://localhost/api/v1/rag/documents'));

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/rag/documents');
  });

  it('returns a structured error and the backend status when the backend response is not ok', async () => {
    mockFetch({ ok: false, status: 404 });

    const { GET } = await import('../../../../../../src/app/api/v1/rag/documents/route');
    const response = (await GET(makeRequest('http://localhost/api/v1/rag/documents'))) as Response;

    expect(response.status).toBe(404);
    const body = await response.json();
    expect(body).toEqual({ error: 'Backend returned 404' });
  });

  it('returns 502 with structured error when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import('../../../../../../src/app/api/v1/rag/documents/route');
    const response = (await GET(makeRequest('http://localhost/api/v1/rag/documents'))) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to fetch documents from backend' });
  });

  it('returns 502 when the backend response body is not valid JSON on the success path', async () => {
    mockFetch({
      ok: true,
      status: 200,
      json: vi.fn().mockRejectedValue(new Error('invalid JSON')),
    });

    const { GET } = await import('../../../../../../src/app/api/v1/rag/documents/route');
    const response = (await GET(makeRequest('http://localhost/api/v1/rag/documents'))) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to fetch documents from backend' });
  });
});
