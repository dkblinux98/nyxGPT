/**
 * Tests for the /api/v1/cloud/state/versions Next.js proxy route (#3510).
 *
 * The version list is the rollback story's index -- if it silently came back
 * empty or truncated, an operator would conclude there is nothing to restore.
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
  return new Request(`http://localhost/api/v1/cloud/state/versions${query}`);
}

describe('/api/v1/cloud/state/versions proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET request to backend using default URL and returns 200', async () => {
    mockFetch({
      ok: true,
      status: 200,
      data: { versions: [{ version_id: 'v2', latest: true }, { version_id: 'v1', latest: false }] },
    });

    const { GET } = await import(
      '../../../../../../../src/app/api/v1/cloud/state/versions/route'
    );
    const response = (await GET(req(), undefined)) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/cloud/state/versions');
    expect(calledOptions.method).toBe('GET');
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({
      versions: [
        { version_id: 'v2', latest: true },
        { version_id: 'v1', latest: false },
      ],
    });
  });

  it('forwards an explicit limit, URL-encoded', async () => {
    mockFetch({ ok: true, status: 200, data: { versions: [] } });

    const { GET } = await import(
      '../../../../../../../src/app/api/v1/cloud/state/versions/route'
    );
    await GET(req('?limit=5'), undefined);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/cloud/state/versions?limit=5');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, data: { versions: [] } });

    const { GET } = await import(
      '../../../../../../../src/app/api/v1/cloud/state/versions/route'
    );
    await GET(req(), undefined);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/cloud/state/versions');
  });

  it('passes through backend response status and body when listing is refused', async () => {
    mockFetch({ ok: false, status: 409, data: { detail: 'state is still local' } });

    const { GET } = await import(
      '../../../../../../../src/app/api/v1/cloud/state/versions/route'
    );
    const response = (await GET(req(), undefined)) as Response;

    expect(response.status).toBe(409);
    const body = await response.json();
    expect(body).toEqual({ detail: 'state is still local' });
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import(
      '../../../../../../../src/app/api/v1/cloud/state/versions/route'
    );
    const response = (await GET(req(), undefined)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to list Terraform state versions' });
  });

  it('returns 502 when the backend response body is not valid JSON', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.reject(new Error('invalid JSON')),
    });

    const { GET } = await import(
      '../../../../../../../src/app/api/v1/cloud/state/versions/route'
    );
    const response = (await GET(req(), undefined)) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to list Terraform state versions' });
  });
});
