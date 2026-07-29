/**
 * Tests for the /api/v1/infra/restart-status Next.js proxy route (#3407).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { ok: boolean; status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

describe('/api/v1/infra/restart-status proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET request to backend using default URL and returns 200', async () => {
    mockFetch({
      ok: true,
      status: 200,
      data: { pending: { api: { keys: ['api.port'], since: 123 } } },
    });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/infra/restart-status/route'
    );
    const response = (await GET()) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/infra/restart-status');
    expect(calledOptions.method).toBe('GET');

    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual({ pending: { api: { keys: ['api.port'], since: 123 } } });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, data: { pending: {} } });

    const { GET } = await import(
      '../../../../../../src/app/api/v1/infra/restart-status/route'
    );
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/infra/restart-status');
  });

  it('returns 502 with structured error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import(
      '../../../../../../src/app/api/v1/infra/restart-status/route'
    );
    const response = (await GET()) as Response;

    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body).toEqual({ error: 'Failed to fetch restart-required status from backend' });
  });
});
