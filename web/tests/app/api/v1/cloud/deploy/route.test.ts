/**
 * Tests for the /api/v1/cloud/deploy Next.js proxy route (#3513).
 *
 * GET is what the dashboard reads to decide whether anything is installed and
 * whether the access tunnel is open; POST spends real money and installs a
 * release on a real instance. Both must forward the caller's inputs verbatim
 * and never let a backend failure read like a quiet "nothing deployed".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { ok: boolean; status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

function getReq() {
  return new Request('http://localhost/api/v1/cloud/deploy');
}

function postReq(body: unknown = {}) {
  return new Request('http://localhost/api/v1/cloud/deploy', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

describe('/api/v1/cloud/deploy proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  describe('GET', () => {
    it('forwards GET to the backend using the default URL and returns 200', async () => {
      mockFetch({
        ok: true,
        status: 200,
        data: {
          deployed: true,
          version: '3.0.0',
          tunnel: { running: true, pid: 4242 },
          urls: { api: 'http://localhost:8000' },
        },
      });

      const { GET } = await import('../../../../../../src/app/api/v1/cloud/deploy/route');
      const response = (await GET(getReq(), undefined)) as Response;

      expect(global.fetch).toHaveBeenCalledTimes(1);
      const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
      expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/cloud/deploy');
      expect(calledOptions.method).toBe('GET');
      expect(calledOptions.cache).toBe('no-store');

      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({
        deployed: true,
        version: '3.0.0',
        tunnel: { running: true, pid: 4242 },
        urls: { api: 'http://localhost:8000' },
      });
    });

    it('uses NYXGPT_API_BASE_URL when set', async () => {
      process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
      mockFetch({ ok: true, status: 200, data: { deployed: false } });

      const { GET } = await import('../../../../../../src/app/api/v1/cloud/deploy/route');
      await GET(getReq(), undefined);

      const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
      expect(calledUrl).toBe('http://custom-backend:9000/api/v1/cloud/deploy');
    });

    it('passes through the backend status and body when the backend refuses', async () => {
      mockFetch({ ok: false, status: 409, data: { error: 'no substrate provisioned' } });

      const { GET } = await import('../../../../../../src/app/api/v1/cloud/deploy/route');
      const response = (await GET(getReq(), undefined)) as Response;

      expect(response.status).toBe(409);
      expect(await response.json()).toEqual({ error: 'no substrate provisioned' });
    });

    it('returns 502 with a structured error when the backend is unreachable', async () => {
      global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

      const { GET } = await import('../../../../../../src/app/api/v1/cloud/deploy/route');
      const response = (await GET(getReq(), undefined)) as Response;

      expect(response.status).toBe(502);
      expect(await response.json()).toEqual({
        error: 'Failed to fetch cloud deployment status from backend',
      });
    });

    it('returns 502 when the backend response body is not valid JSON', async () => {
      global.fetch = vi.fn().mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: () => Promise.reject(new Error('invalid JSON')),
      });

      const { GET } = await import('../../../../../../src/app/api/v1/cloud/deploy/route');
      const response = (await GET(getReq(), undefined)) as Response;

      expect(response.status).toBe(502);
      expect(await response.json()).toEqual({
        error: 'Failed to fetch cloud deployment status from backend',
      });
    });
  });

  describe('POST', () => {
    it('forwards the deploy request and body to the backend', async () => {
      mockFetch({
        ok: true,
        status: 200,
        data: { action: 'deploy', plan: { version: '3.0.0' } },
      });

      const { POST } = await import('../../../../../../src/app/api/v1/cloud/deploy/route');
      const response = (await POST(
        postReq({ region: 'us-east-1', version: '3.0.0', skip_observability: true }),
        undefined
      )) as Response;

      const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
      expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/cloud/deploy');
      expect(calledOptions.method).toBe('POST');
      expect(JSON.parse(calledOptions.body)).toEqual({
        region: 'us-east-1',
        version: '3.0.0',
        skip_observability: true,
      });

      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({ action: 'deploy', plan: { version: '3.0.0' } });
    });

    it('uses NYXGPT_API_BASE_URL when set', async () => {
      process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
      mockFetch({ ok: true, status: 200 });

      const { POST } = await import('../../../../../../src/app/api/v1/cloud/deploy/route');
      await POST(postReq(), undefined);

      const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
      expect(calledUrl).toBe('http://custom-backend:9000/api/v1/cloud/deploy');
    });

    it('falls back to an empty body when the request JSON is invalid', async () => {
      mockFetch({ ok: true, status: 200 });

      const { POST } = await import('../../../../../../src/app/api/v1/cloud/deploy/route');
      const badReq = new Request('http://localhost/api/v1/cloud/deploy', {
        method: 'POST',
        body: 'not json',
      });
      await POST(badReq, undefined);

      const calledOptions = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1];
      expect(JSON.parse(calledOptions.body)).toEqual({});
    });

    it('passes through the backend status and body when the deploy is refused', async () => {
      mockFetch({ ok: false, status: 409, data: { detail: 'instance never accepted SSH' } });

      const { POST } = await import('../../../../../../src/app/api/v1/cloud/deploy/route');
      const response = (await POST(postReq(), undefined)) as Response;

      expect(response.status).toBe(409);
      expect(await response.json()).toEqual({ detail: 'instance never accepted SSH' });
    });

    it('returns 502 with a structured error when the backend is unreachable', async () => {
      global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

      const { POST } = await import('../../../../../../src/app/api/v1/cloud/deploy/route');
      const response = (await POST(postReq(), undefined)) as Response;

      expect(response.status).toBe(502);
      expect(await response.json()).toEqual({ error: 'Failed to deploy the cloud stack' });
    });

    it('returns 502 when the backend response body is not valid JSON', async () => {
      global.fetch = vi.fn().mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: () => Promise.reject(new Error('invalid JSON')),
      });

      const { POST } = await import('../../../../../../src/app/api/v1/cloud/deploy/route');
      const response = (await POST(postReq(), undefined)) as Response;

      expect(response.status).toBe(502);
      expect(await response.json()).toEqual({ error: 'Failed to deploy the cloud stack' });
    });
  });
});
