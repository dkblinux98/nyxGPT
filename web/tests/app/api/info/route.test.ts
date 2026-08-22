/**
 * Tests for the /api/info Next.js proxy route.
 *
 * Issue #3245: bring vitest coverage for API proxy routes to 100%. GET
 * forwards to the backend and passes through status/body.
 *
 * Issue #3982: it also stamps the *web* tier's own version into the payload
 * on the way through. This route is the one place both tiers are present at
 * once -- it runs in the Next.js server and is talking to the API -- so the
 * client receives a consistent pair from a single request instead of having
 * to assemble one.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

function mockFetch(response: { ok: boolean; status: number; body?: string }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: response.ok,
    status: response.status,
    body: null,
    text: async () => response.body ?? '',
    headers: new Headers({ 'Content-Type': 'application/json' }),
  });
}

describe('/api/info GET route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
    delete process.env.NYXGPT_WEB_VERSION;
  });

  afterEach(() => {
    delete process.env.NYXGPT_WEB_VERSION;
  });

  it('forwards GET to backend info endpoint', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../src/app/api/info/route');
    await GET();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/info');
    expect(calledOptions.cache).toBe('no-store');
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../src/app/api/info/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/info');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200 });

    const { GET } = await import('../../../../src/app/api/info/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/info');
  });

  it('passes through backend response status', async () => {
    mockFetch({ ok: false, status: 500 });

    const { GET } = await import('../../../../src/app/api/info/route');
    const response = await GET() as Response;

    expect(response.status).toBe(500);
    expect(response.headers.get('Content-Type')).toBe('application/json');
  });

  it('stamps the web tier version alongside the API version (#3982)', async () => {
    process.env.NYXGPT_WEB_VERSION = '2.1.0';
    mockFetch({ ok: true, status: 200, body: JSON.stringify({ release_version: '3.0.0' }) });

    const { GET } = await import('../../../../src/app/api/info/route');
    const body = await ((await GET()) as Response).json();

    // Both true running versions, from one request: this pair is what makes
    // the 2.1.0-web/3.0.0-API stack visible instead of silent.
    expect(body.release_version).toBe('3.0.0');
    expect(body.web_version).toBe('2.1.0');
    expect(body.web_version_source).toBe('env');
  });

  it('reports an undeterminable web version as unknown, never as the API version', async () => {
    mockFetch({ ok: true, status: 200, body: JSON.stringify({ release_version: '3.0.0' }) });

    const { GET } = await import('../../../../src/app/api/info/route');
    const body = await ((await GET()) as Response).json();

    expect(body.web_version).toBeNull();
    expect(body.web_version_source).toBe('unknown');
  });

  it('forwards a non-JSON upstream body untouched', async () => {
    // An HTML error page from something in front of the API still has to
    // reach the operator as-is, not be replaced by a synthesised payload.
    mockFetch({ ok: false, status: 502, body: '<html>bad gateway</html>' });

    const { GET } = await import('../../../../src/app/api/info/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(502);
    expect(await response.text()).toBe('<html>bad gateway</html>');
  });
});
