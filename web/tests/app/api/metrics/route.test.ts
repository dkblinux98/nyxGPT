/**
 * Tests for the /api/v1/metrics Next.js proxy route.
 *
 * Regression test for acceptance failure on issue #3122:
 * - web/src/components/ResourceMetrics.tsx calls /api/v1/metrics (frontend path)
 * - The proxy at web/src/app/api/v1/metrics/route.ts was missing, causing 404.
 * - The proxy must forward requests to the backend at /api/v1/metrics.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockMetricsData = {
  memory: { rss_mb: 256.5, vms_mb: 512.0, percent: 15.2, available_mb: 8192.0 },
  cpu: { process_percent: 5.3, system_percent: 25.8 },
  latency: { avg_ms: 45.2, p50_ms: 42.0, p95_ms: 85.5, p99_ms: 120.3 },
  queue: { depth: 3, total_requests: 1250 },
};

describe('/api/v1/metrics proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('calls backend /api/v1/metrics endpoint (not bare /metrics)', async () => {
    // Regression: the missing proxy caused 404. Verify the route exists and
    // forwards to the correct backend path with the /api/v1 prefix.
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: null,
      headers: new Headers({ 'Content-Type': 'application/json' }),
    });

    const { GET } = await import('../../../../src/app/api/v1/metrics/route');
    await GET();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toContain('/api/v1/metrics');
    // Must NOT match the buggy pattern: http://host/metrics (no /api/v1 prefix)
    expect(calledUrl).not.toMatch(/^https?:\/\/[^/]+\/metrics$/);
  });

  it('uses default backend URL when NYXGPT_API_BASE_URL is not set', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: null,
      headers: new Headers({ 'Content-Type': 'application/json' }),
    });

    const { GET } = await import('../../../../src/app/api/v1/metrics/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/metrics');
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';

    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: null,
      headers: new Headers({ 'Content-Type': 'application/json' }),
    });

    const { GET } = await import('../../../../src/app/api/v1/metrics/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/metrics');
  });

  it('passes through backend response status', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 503,
      body: null,
      headers: new Headers({ 'Content-Type': 'application/json' }),
    });

    const { GET } = await import('../../../../src/app/api/v1/metrics/route');
    const response = await GET() as Response;

    expect(response.status).toBe(503);
  });
});
