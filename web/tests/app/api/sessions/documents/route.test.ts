/**
 * Tests for the /api/sessions/[name]/documents and
 * /api/sessions/[name]/documents/[doc_id] Next.js proxy routes.
 *
 * Issue #3130: Add tests for the session document attachment API routes
 * introduced in #3120 (GET, POST on /documents and DELETE on /documents/[doc_id]).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const ATTACHED_DOCS_RESPONSE = { attached_doc_ids: ['doc-a', 'doc-b'] };
const EMPTY_DOCS_RESPONSE = { attached_doc_ids: [] };

function mockFetch(response: { ok: boolean; status: number; body?: object | null }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: response.ok,
    status: response.status,
    body: response.body !== undefined ? response.body : null,
    headers: new Headers({ 'Content-Type': 'application/json' }),
  });
}

describe('/api/sessions/[name]/documents GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to backend session documents endpoint', async () => {
    mockFetch({ ok: true, status: 200, body: null });

    const { GET } = await import(
      '../../../../../src/app/api/sessions/[name]/documents/route'
    );
    await GET(new Request('http://localhost/api/sessions/my-session/documents'), {
      params: Promise.resolve({ name: 'my-session' }),
    });

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toContain('/api/v1/sessions/my-session/documents');
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200, body: null });

    const { GET } = await import(
      '../../../../../src/app/api/sessions/[name]/documents/route'
    );
    await GET(new Request('http://localhost/api/sessions/test/documents'), {
      params: Promise.resolve({ name: 'test' }),
    });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/sessions/test/documents');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, body: null });

    const { GET } = await import(
      '../../../../../src/app/api/sessions/[name]/documents/route'
    );
    await GET(new Request('http://localhost/api/sessions/test/documents'), {
      params: Promise.resolve({ name: 'test' }),
    });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/sessions/test/documents');
  });

  it('URL-encodes session name', async () => {
    mockFetch({ ok: true, status: 200, body: null });

    const { GET } = await import(
      '../../../../../src/app/api/sessions/[name]/documents/route'
    );
    await GET(new Request('http://localhost/api/sessions/my%20session/documents'), {
      params: Promise.resolve({ name: 'my%20session' }),
    });

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toContain('my%20session');
  });

  it('passes through backend response status', async () => {
    mockFetch({ ok: false, status: 404, body: null });

    const { GET } = await import(
      '../../../../../src/app/api/sessions/[name]/documents/route'
    );
    const response = await GET(
      new Request('http://localhost/api/sessions/missing/documents'),
      { params: Promise.resolve({ name: 'missing' }) }
    ) as Response;

    expect(response.status).toBe(404);
  });
});

describe('/api/sessions/[name]/documents POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST with body to backend session documents endpoint', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: null,
      headers: new Headers({ 'Content-Type': 'application/json' }),
    });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/documents/route'
    );
    const req = new Request('http://localhost/api/sessions/my-session/documents', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc_id: 'doc-a' }),
    });
    await POST(req, { params: Promise.resolve({ name: 'my-session' }) });

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/sessions/my-session/documents');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ doc_id: 'doc-a' });
  });

  it('passes through backend response status on POST', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 422,
      body: null,
      headers: new Headers({ 'Content-Type': 'application/json' }),
    });

    const { POST } = await import(
      '../../../../../src/app/api/sessions/[name]/documents/route'
    );
    const req = new Request('http://localhost/api/sessions/my-session/documents', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc_id: 'bad-doc' }),
    });
    const response = await POST(req, {
      params: Promise.resolve({ name: 'my-session' }),
    }) as Response;

    expect(response.status).toBe(422);
  });
});

describe('/api/sessions/[name]/documents/[doc_id] DELETE route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards DELETE to backend session document endpoint', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: null,
      headers: new Headers({ 'Content-Type': 'application/json' }),
    });

    const { DELETE } = await import(
      '../../../../../src/app/api/sessions/[name]/documents/[doc_id]/route'
    );
    await DELETE(
      new Request('http://localhost/api/sessions/my-session/documents/doc-a'),
      { params: Promise.resolve({ name: 'my-session', doc_id: 'doc-a' }) }
    );

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/sessions/my-session/documents/doc-a');
    expect(calledOptions.method).toBe('DELETE');
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: null,
      headers: new Headers({ 'Content-Type': 'application/json' }),
    });

    const { DELETE } = await import(
      '../../../../../src/app/api/sessions/[name]/documents/[doc_id]/route'
    );
    await DELETE(
      new Request('http://localhost/api/sessions/test/documents/doc-x'),
      { params: Promise.resolve({ name: 'test', doc_id: 'doc-x' }) }
    );

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe(
      'http://127.0.0.1:8000/api/v1/sessions/test/documents/doc-x'
    );
  });

  it('URL-encodes both session name and doc_id', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: null,
      headers: new Headers({ 'Content-Type': 'application/json' }),
    });

    const { DELETE } = await import(
      '../../../../../src/app/api/sessions/[name]/documents/[doc_id]/route'
    );
    await DELETE(
      new Request('http://localhost/api/sessions/my%20session/documents/doc%2Fid'),
      {
        params: Promise.resolve({
          name: 'my%20session',
          doc_id: 'doc%2Fid',
        }),
      }
    );

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toContain('my%20session');
    expect(calledUrl).toContain('doc%2Fid');
  });

  it('passes through backend response status on DELETE', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 404,
      body: null,
      headers: new Headers({ 'Content-Type': 'application/json' }),
    });

    const { DELETE } = await import(
      '../../../../../src/app/api/sessions/[name]/documents/[doc_id]/route'
    );
    const response = await DELETE(
      new Request('http://localhost/api/sessions/s/documents/missing-doc'),
      { params: Promise.resolve({ name: 's', doc_id: 'missing-doc' }) }
    ) as Response;

    expect(response.status).toBe(404);
  });
});
