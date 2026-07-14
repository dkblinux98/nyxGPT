/**
 * Tests for the /api/prometheus-metrics Next.js proxy route.
 *
 * This proxies the backend's Prometheus-format `/metrics` endpoint (bare,
 * no /api/v1 prefix, text exposition format) so the admin dashboard can
 * link to it without needing the browser to know the backend's host/port.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

describe('/api/prometheus-metrics proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('calls backend bare /metrics endpoint (not /api/v1/metrics)', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: null,
      headers: new Headers({ 'Content-Type': 'text/plain; version=0.0.4; charset=utf-8' }),
    });

    const { GET } = await import('../../../../src/app/api/prometheus-metrics/route');
    await GET();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/metrics');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';

    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: null,
      headers: new Headers({ 'Content-Type': 'text/plain' }),
    });

    const { GET } = await import('../../../../src/app/api/prometheus-metrics/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/metrics');
  });

  it('passes through backend response status and content-type', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: null,
      headers: new Headers({ 'Content-Type': 'text/plain; version=0.0.4; charset=utf-8' }),
    });

    const { GET } = await import('../../../../src/app/api/prometheus-metrics/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('text/plain; version=0.0.4; charset=utf-8');
  });

  it('returns 502 with a plain-text error when backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import('../../../../src/app/api/prometheus-metrics/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(502);
    const body = await response.text();
    expect(body).toContain('metrics backend unavailable');
  });
});
