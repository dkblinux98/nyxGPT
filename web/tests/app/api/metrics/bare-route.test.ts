/**
 * Tests for the /api/metrics Next.js proxy route (src/app/api/metrics/route.ts).
 *
 * Distinct from /api/v1/metrics (covered by route.test.ts in this directory):
 * this route forwards to the backend's /api/v1/metrics endpoint and returns
 * NextResponse.json, with its own try/catch error handling.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

describe('/api/metrics proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards to the backend /api/v1/metrics endpoint and returns the JSON body', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue({ cpu: 42 }),
    });

    const { GET } = await import('../../../../src/app/api/metrics/route');
    const response = await GET();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(calledUrl).toContain('/api/v1/metrics');
    expect(calledOptions.method).toBe('GET');
    const headers = calledOptions.headers as Headers;
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ cpu: 42 });
  });

  it('passes through a non-ok backend status with a structured error body', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 503,
      json: vi.fn(),
    });

    const { GET } = await import('../../../../src/app/api/metrics/route');
    const response = await GET();

    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: 'Backend API returned 503' });
  });

  it('returns 500 with a structured error when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import('../../../../src/app/api/metrics/route');
    const response = await GET();

    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ error: 'Failed to fetch resource metrics' });
  });
});
