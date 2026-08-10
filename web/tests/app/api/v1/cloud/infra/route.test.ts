/**
 * Tests for the /api/v1/cloud/infra Next.js proxy route (#3509).
 *
 * This is the read-only surface: it reports whether the AWS substrate is
 * provisioned and what the security group actually allows, so a backend that
 * is down must not be able to look like "nothing is open".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { ok: boolean; status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

function req() {
  return new Request('http://localhost/api/v1/cloud/infra');
}

describe('/api/v1/cloud/infra proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET request to backend using default URL and returns 200', async () => {
    mockFetch({
      ok: true,
      status: 200,
      data: { provisioned: true, access_model: { open_ports: [22] } },
    });

    const { GET } = await import('../../../../../../src/app/api/v1/cloud/infra/route');
    const response = (await GET(req(), undefined)) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/cloud/infra');
    expect(calledOptions.method).toBe('GET');
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ provisioned: true, access_model: { open_ports: [22] } });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, data: { provisioned: false } });

    const { GET } = await import('../../../../../../src/app/api/v1/cloud/infra/route');
    await GET(req(), undefined);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/cloud/infra');
  });

  it('passes through backend response status and body when backend errors', async () => {
    mockFetch({ ok: false, status: 503, data: { error: 'terraform not installed' } });

    const { GET } = await import('../../../../../../src/app/api/v1/cloud/infra/route');
    const response = (await GET(req(), undefined)) as Response;

    expect(response.status).toBe(503);
    const body = await response.json();
    expect(body).toEqual({ error: 'terraform not installed' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import('../../../../../../src/app/api/v1/cloud/infra/route');
    const response = (await GET(req(), undefined)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to fetch cloud substrate status from backend' });
  });

  it('returns 502 when the backend response body is not valid JSON', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.reject(new Error('invalid JSON')),
    });

    const { GET } = await import('../../../../../../src/app/api/v1/cloud/infra/route');
    const response = (await GET(req(), undefined)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to fetch cloud substrate status from backend' });
  });
});
