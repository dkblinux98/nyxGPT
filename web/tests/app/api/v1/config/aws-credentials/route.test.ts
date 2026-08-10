/**
 * Tests for the /api/v1/config/aws-credentials Next.js proxy route (#3512).
 *
 * web/src/app/api/v1/config/aws-credentials/route.ts forwards GET/POST to the
 * backend /api/v1/config/aws-credentials endpoint via apiFetch, passing
 * r.body straight through.
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

describe('/api/v1/config/aws-credentials GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/config/aws-credentials/route');
    await GET();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/config/aws-credentials');
  });

  it('passes through a successful backend response status', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../../../src/app/api/v1/config/aws-credentials/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(200);
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import('../../../../../../src/app/api/v1/config/aws-credentials/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'config backend unavailable' });
  });
});

describe('/api/v1/config/aws-credentials POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST body and method to backend', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import('../../../../../../src/app/api/v1/config/aws-credentials/route');
    const req = new Request('http://localhost/api/v1/config/aws-credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ destination: 'ambient', profile: 'nyxgpt', region: 'us-east-1' }),
    });
    await POST(req);

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/config/aws-credentials');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({
      destination: 'ambient',
      profile: 'nyxgpt',
      region: 'us-east-1',
    });
  });

  it('passes through a non-2xx backend response status', async () => {
    mockFetch({ ok: false, status: 422 });

    const { POST } = await import('../../../../../../src/app/api/v1/config/aws-credentials/route');
    const req = new Request('http://localhost/api/v1/config/aws-credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ destination: 'ambient', profile: 'nyxgpt', region: 'not-a-region' }),
    });
    const response = (await POST(req)) as Response;

    expect(response.status).toBe(422);
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import('../../../../../../src/app/api/v1/config/aws-credentials/route');
    const req = new Request('http://localhost/api/v1/config/aws-credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ destination: 'ambient', profile: 'nyxgpt', region: 'us-east-1' }),
    });
    const response = (await POST(req)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'config backend unavailable' });
  });
});
