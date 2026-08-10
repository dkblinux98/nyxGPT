/**
 * Tests for the /api/v1/config/aws-credentials/secret-store Next.js proxy
 * route (#3512).
 *
 * web/src/app/api/v1/config/aws-credentials/secret-store/route.ts forwards
 * POST to the backend /api/v1/config/aws-credentials/secret-store endpoint
 * via apiFetch, passing r.body straight through.
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

describe('/api/v1/config/aws-credentials/secret-store POST route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards POST body and method to backend', async () => {
    mockFetch({ ok: true, status: 200 });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/config/aws-credentials/secret-store/route'
    );
    const req = new Request('http://localhost/api/v1/config/aws-credentials/secret-store', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: 'ssm', ssm_prefix: '/nyxgpt' }),
    });
    await POST(req);

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/config/aws-credentials/secret-store');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({ provider: 'ssm', ssm_prefix: '/nyxgpt' });
  });

  it('passes through a non-2xx backend response status', async () => {
    mockFetch({ ok: false, status: 422 });

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/config/aws-credentials/secret-store/route'
    );
    const req = new Request('http://localhost/api/v1/config/aws-credentials/secret-store', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: 'vault' }),
    });
    const response = (await POST(req)) as Response;

    expect(response.status).toBe(422);
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import(
      '../../../../../../../src/app/api/v1/config/aws-credentials/secret-store/route'
    );
    const req = new Request('http://localhost/api/v1/config/aws-credentials/secret-store', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: 'ssm' }),
    });
    const response = (await POST(req)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'config backend unavailable' });
  });
});
