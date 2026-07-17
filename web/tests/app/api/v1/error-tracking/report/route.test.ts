/**
 * Tests for the /api/v1/error-tracking/report Next.js proxy route (issue #3245).
 *
 * Unlike most proxy routes, this one wraps everything (request JSON parsing,
 * the backend fetch, and parsing the backend's JSON response) in a single
 * try/catch and always degrades to a 202 "unavailable" response on failure,
 * because client-side error reporting must never surface its own errors.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

describe('/api/v1/error-tracking/report proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST body to backend using default URL', async () => {
    mockFetch({ status: 200, data: { received: true } });

    const { POST } = await import(
      '../../../../../../src/app/api/v1/error-tracking/report/route'
    );
    const req = new Request('http://localhost/api/v1/error-tracking/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'error', message: 'boom' }),
    });
    const response = (await POST(req)) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/error-tracking/report');
    expect(calledOptions.method).toBe('POST');
    expect(calledOptions.headers.get('Content-Type')).toBe('application/json');
    expect(JSON.parse(calledOptions.body)).toEqual({ type: 'error', message: 'boom' });

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ received: true });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ status: 200, data: { received: true } });

    const { POST } = await import(
      '../../../../../../src/app/api/v1/error-tracking/report/route'
    );
    const req = new Request('http://localhost/api/v1/error-tracking/report', {
      method: 'POST',
      body: JSON.stringify({ type: 'error' }),
    });
    await POST(req);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/error-tracking/report');
  });

  it('returns 202 unavailable when request JSON is invalid', async () => {
    mockFetch({ status: 200, data: { received: true } });

    const { POST } = await import(
      '../../../../../../src/app/api/v1/error-tracking/report/route'
    );
    const req = new Request('http://localhost/api/v1/error-tracking/report', {
      method: 'POST',
      body: 'not json',
    });
    const response = (await POST(req)) as Response;

    expect(global.fetch).not.toHaveBeenCalled();
    expect(response.status).toBe(202);
    const body = await response.json();
    expect(body).toEqual({ status: 'unavailable' });
  });

  it('passes through a non-2xx backend status', async () => {
    mockFetch({ status: 500, data: { error: 'boom' } });

    const { POST } = await import(
      '../../../../../../src/app/api/v1/error-tracking/report/route'
    );
    const req = new Request('http://localhost/api/v1/error-tracking/report', {
      method: 'POST',
      body: JSON.stringify({ type: 'error' }),
    });
    const response = (await POST(req)) as Response;

    expect(response.status).toBe(500);
    const body = await response.json();
    expect(body).toEqual({ error: 'boom' });
  });

  it('returns 202 unavailable when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import(
      '../../../../../../src/app/api/v1/error-tracking/report/route'
    );
    const req = new Request('http://localhost/api/v1/error-tracking/report', {
      method: 'POST',
      body: JSON.stringify({ type: 'error' }),
    });
    const response = (await POST(req)) as Response;

    expect(response.status).toBe(202);
    const body = await response.json();
    expect(body).toEqual({ status: 'unavailable' });
  });

  it('returns 202 unavailable when the backend response body is not valid JSON', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      status: 200,
      json: () => Promise.reject(new Error('invalid JSON')),
    });

    const { POST } = await import(
      '../../../../../../src/app/api/v1/error-tracking/report/route'
    );
    const req = new Request('http://localhost/api/v1/error-tracking/report', {
      method: 'POST',
      body: JSON.stringify({ type: 'error' }),
    });
    const response = (await POST(req)) as Response;

    expect(response.status).toBe(202);
    const body = await response.json();
    expect(body).toEqual({ status: 'unavailable' });
  });
});
