/**
 * Tests for the /api/v1/cloud/state Next.js proxy route (#3510).
 *
 * The read-only surface: where the substrate's Terraform state lives and how
 * it is locked. `?verify=true` is the expensive flavor that also checks the
 * bucket and lock table against AWS, so it must be forwarded only when the
 * caller actually asked for it -- the dashboard polls this route.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { ok: boolean; status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

function req(query = '') {
  return new Request(`http://localhost/api/v1/cloud/state${query}`);
}

describe('/api/v1/cloud/state proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET request to backend using default URL and returns 200', async () => {
    mockFetch({
      ok: true,
      status: 200,
      data: { backend: 's3', remote_enabled: true, bucket: 'nyxgpt-tfstate' },
    });

    const { GET } = await import('../../../../../../src/app/api/v1/cloud/state/route');
    const response = (await GET(req(), undefined)) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/cloud/state');
    expect(calledOptions.method).toBe('GET');
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ backend: 's3', remote_enabled: true, bucket: 'nyxgpt-tfstate' });
  });

  it('forwards ?verify=true so the caller opts in to the AWS round-trip', async () => {
    mockFetch({ ok: true, status: 200, data: { backend: 's3', bootstrapped: true } });

    const { GET } = await import('../../../../../../src/app/api/v1/cloud/state/route');
    await GET(req('?verify=true'), undefined);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/cloud/state?verify=true');
  });

  it('does not forward a non-"true" verify value', async () => {
    mockFetch({ ok: true, status: 200, data: { backend: 'local' } });

    const { GET } = await import('../../../../../../src/app/api/v1/cloud/state/route');
    await GET(req('?verify=yes'), undefined);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/cloud/state');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, data: { backend: 'local' } });

    const { GET } = await import('../../../../../../src/app/api/v1/cloud/state/route');
    await GET(req(), undefined);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/cloud/state');
  });

  it('passes through backend response status and body when backend errors', async () => {
    mockFetch({ ok: false, status: 409, data: { detail: 'terraform not installed' } });

    const { GET } = await import('../../../../../../src/app/api/v1/cloud/state/route');
    const response = (await GET(req(), undefined)) as Response;

    expect(response.status).toBe(409);
    const body = await response.json();
    expect(body).toEqual({ detail: 'terraform not installed' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import('../../../../../../src/app/api/v1/cloud/state/route');
    const response = (await GET(req(), undefined)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to fetch Terraform state status from backend' });
  });

  it('returns 502 when the backend response body is not valid JSON', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.reject(new Error('invalid JSON')),
    });

    const { GET } = await import('../../../../../../src/app/api/v1/cloud/state/route');
    const response = (await GET(req(), undefined)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to fetch Terraform state status from backend' });
  });
});
