/**
 * Tests for the /api/rag/upload Next.js proxy route (issue #3245).
 *
 * Note: this route lives outside /api/v1 (bare /api/rag/upload). It reads
 * the incoming request as FormData (multipart upload) and forwards it
 * as-is to the backend, then streams the backend's response body straight
 * back through. There is no try/catch and no branching in the handler, so
 * full coverage just requires exercising the straight-line path with the
 * default and a custom backend URL, plus a non-2xx status passthrough.
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

function formDataRequest(url: string) {
  const formData = new FormData();
  formData.append('file', new Blob(['hello world'], { type: 'text/plain' }), 'hello.txt');
  return new Request(url, {
    method: 'POST',
    body: formData,
  });
}

describe('/api/rag/upload proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards the multipart FormData body to the backend using the default URL', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../../src/app/api/rag/upload/route');
    const request = formDataRequest('http://localhost/api/rag/upload');
    const response = (await POST(request)) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/rag/upload');
    expect(calledOptions.method).toBe('POST');
    expect(calledOptions.body).toBeInstanceOf(FormData);
    expect(calledOptions.body.get('file')).toBeTruthy();

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../../src/app/api/rag/upload/route');
    const request = formDataRequest('http://localhost/api/rag/upload');
    await POST(request);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/rag/upload');
  });

  it('passes through a non-2xx backend status', async () => {
    mockFetch({ ok: false, status: 413 });

    const { POST } = await import('../../../../../src/app/api/rag/upload/route');
    const request = formDataRequest('http://localhost/api/rag/upload');
    const response = (await POST(request)) as Response;

    expect(response.status).toBe(413);
  });
});
