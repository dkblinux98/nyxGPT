/**
 * Tests for the /api/v1/rag/index-repo Next.js proxy route (issue #3245).
 *
 * The handler performs extensive request-body validation (repo_path,
 * extensions, extract_docs_only, ensure_schema) before ever calling the
 * backend, then wraps the backend call in try/catch. Covers every
 * validation branch, the backend ok/non-ok paths (including the
 * `.catch()` fallback when the error body isn't valid JSON), and the
 * outer catch for both an unparseable request body and a network failure.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(response: { ok: boolean; status: number; json?: () => Promise<unknown> }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: response.ok,
    status: response.status,
    json: response.json ?? vi.fn().mockResolvedValue({}),
  });
}

function jsonRequest(body: unknown) {
  return new Request('http://localhost/api/v1/rag/index-repo', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

describe('/api/v1/rag/index-repo POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('returns 400 when repo_path is missing', async () => {
    global.fetch = vi.fn();
    const { POST } = await import('../../../../../../src/app/api/v1/rag/index-repo/route');
    const response = (await POST(jsonRequest({}))) as Response;

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body).toEqual({ detail: 'repo_path is required and must be a string' });
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('returns 400 when repo_path is not a string', async () => {
    global.fetch = vi.fn();
    const { POST } = await import('../../../../../../src/app/api/v1/rag/index-repo/route');
    const response = (await POST(jsonRequest({ repo_path: 42 }))) as Response;

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body).toEqual({ detail: 'repo_path is required and must be a string' });
  });

  it('returns 400 when repo_path is empty after trimming', async () => {
    global.fetch = vi.fn();
    const { POST } = await import('../../../../../../src/app/api/v1/rag/index-repo/route');
    const response = (await POST(jsonRequest({ repo_path: '   ' }))) as Response;

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body).toEqual({ detail: 'repo_path cannot be empty' });
  });

  it('returns 400 when extensions is provided but not an array', async () => {
    global.fetch = vi.fn();
    const { POST } = await import('../../../../../../src/app/api/v1/rag/index-repo/route');
    const response = (await POST(
      jsonRequest({ repo_path: '/repo', extensions: '.py' })
    )) as Response;

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body).toEqual({ detail: 'extensions must be an array' });
  });

  it('returns 400 when an extension entry is not a string', async () => {
    global.fetch = vi.fn();
    const { POST } = await import('../../../../../../src/app/api/v1/rag/index-repo/route');
    const response = (await POST(
      jsonRequest({ repo_path: '/repo', extensions: ['.py', 7] })
    )) as Response;

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body).toEqual({ detail: 'all extensions must be strings' });
  });

  it('returns 400 when extract_docs_only is not a boolean', async () => {
    global.fetch = vi.fn();
    const { POST } = await import('../../../../../../src/app/api/v1/rag/index-repo/route');
    const response = (await POST(
      jsonRequest({ repo_path: '/repo', extract_docs_only: 'yes' })
    )) as Response;

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body).toEqual({ detail: 'extract_docs_only must be a boolean' });
  });

  it('returns 400 when ensure_schema is not a boolean', async () => {
    global.fetch = vi.fn();
    const { POST } = await import('../../../../../../src/app/api/v1/rag/index-repo/route');
    const response = (await POST(
      jsonRequest({ repo_path: '/repo', ensure_schema: 'yes' })
    )) as Response;

    expect(response.status).toBe(400);
    const body = await response.json();
    expect(body).toEqual({ detail: 'ensure_schema must be a boolean' });
  });

  it('accepts a fully valid body (extensions array, both booleans) and forwards it, defaulting to the default backend URL', async () => {
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ indexed: 12 }) });

    const { POST } = await import('../../../../../../src/app/api/v1/rag/index-repo/route');
    const response = (await POST(
      jsonRequest({
        repo_path: '/repo',
        extensions: ['.py', '.md'],
        extract_docs_only: true,
        ensure_schema: false,
      })
    )) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/rag/index-repo');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({
      repo_path: '/repo',
      extensions: ['.py', '.md'],
      extract_docs_only: true,
      ensure_schema: false,
    });

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ indexed: 12 });
  });

  it('accepts a body with extensions explicitly null (skips the extensions checks)', async () => {
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ indexed: 0 }) });

    const { POST } = await import('../../../../../../src/app/api/v1/rag/index-repo/route');
    const response = (await POST(
      jsonRequest({ repo_path: '/repo', extensions: null })
    )) as Response;

    expect(response.status).toBe(200);
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, json: vi.fn().mockResolvedValue({}) });

    const { POST } = await import('../../../../../../src/app/api/v1/rag/index-repo/route');
    await POST(jsonRequest({ repo_path: '/repo' }));

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/rag/index-repo');
  });

  it('passes through backend error JSON when the backend responds non-ok', async () => {
    mockFetch({
      ok: false,
      status: 422,
      json: vi.fn().mockResolvedValue({ detail: 'invalid repo' }),
    });

    const { POST } = await import('../../../../../../src/app/api/v1/rag/index-repo/route');
    const response = (await POST(jsonRequest({ repo_path: '/repo' }))) as Response;

    expect(response.status).toBe(422);
    const body = await response.json();
    expect(body).toEqual({ detail: 'invalid repo' });
  });

  it('falls back to a synthetic error body when the backend error response has no JSON', async () => {
    mockFetch({
      ok: false,
      status: 500,
      json: vi.fn().mockRejectedValue(new Error('no body')),
    });

    const { POST } = await import('../../../../../../src/app/api/v1/rag/index-repo/route');
    const response = (await POST(jsonRequest({ repo_path: '/repo' }))) as Response;

    expect(response.status).toBe(500);
    const body = await response.json();
    expect(body).toEqual({ error: 'Unknown error' });
  });

  it('returns 502 when the request body is not valid JSON', async () => {
    global.fetch = vi.fn();
    const { POST } = await import('../../../../../../src/app/api/v1/rag/index-repo/route');
    const request = new Request('http://localhost/api/v1/rag/index-repo', {
      method: 'POST',
      body: 'not json',
    });
    const response = (await POST(request)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ detail: 'Failed to index repository via backend' });
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('returns 502 with structured error when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import('../../../../../../src/app/api/v1/rag/index-repo/route');
    const response = (await POST(jsonRequest({ repo_path: '/repo' }))) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ detail: 'Failed to index repository via backend' });
  });
});
