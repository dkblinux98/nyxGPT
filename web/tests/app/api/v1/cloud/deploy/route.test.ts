/**
 * Tests for the /api/v1/cloud/deploy Next.js proxy route (#3513, reconciled to
 * read-only by #3514).
 *
 * GET is what the dashboard reads to decide whether anything is installed,
 * whether the access tunnel is open, and what has happened to the deployment.
 * It must forward the caller's inputs verbatim and never let a backend failure
 * read like a quiet "nothing deployed". There is deliberately no POST: the
 * owner's decision on #3514 keeps deploy and teardown on the CLI, so the
 * browser has no path to one.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

function mockFetch(opts: { ok: boolean; status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

function getReq(query = '') {
  return new Request(`http://localhost/api/v1/cloud/deploy${query}`);
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

    it('forwards an explicit health probe to the backend (#3514)', async () => {
      mockFetch({ ok: true, status: 200, data: { deployed: true } });

      const { GET } = await import('../../../../../../src/app/api/v1/cloud/deploy/route');
      await GET(getReq('?probe_health=true'), undefined);

      const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
      expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/cloud/deploy?probe_health=true');
    });

    it('does not ask for a probe when the caller did not', async () => {
      // The unprobed status is the side-effect-free read; adding a probe here
      // would put a network call behind every poll.
      mockFetch({ ok: true, status: 200, data: { deployed: true } });

      const { GET } = await import('../../../../../../src/app/api/v1/cloud/deploy/route');
      await GET(getReq(), undefined);

      const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
      expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/cloud/deploy');
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

  describe('mutations', () => {
    it('exposes no way for the browser to deploy or tear down', async () => {
      // The owner's #3514 decision put deploy and teardown on the CLI. If a
      // POST handler ever reappears here, the dashboard regains a path to
      // spending real money whether or not any button points at it.
      const route = await import('../../../../../../src/app/api/v1/cloud/deploy/route');

      expect(Object.keys(route)).toEqual(['GET']);
    });
  });
});
