/**
 * Tests for the /api/v1/ops/cloud-artifact-smoke Next.js proxy route (#3784).
 *
 * The route is a pass-through in both directions, and both directions matter:
 * GET is polled by the panel while a run is in flight, and POST is the only
 * way the dashboard can start one (Definition of Done -- an ops feature has to
 * be operable from the SRE surface). What these tests pin is that it never
 * invents an answer: the backend's status and body come through untouched --
 * including the 409 that says a run is already running, which the panel shows
 * the operator -- and an unreachable backend degrades to a structured 502
 * rather than a stack trace or a hang.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const ROUTE = '../../../../../../src/app/api/v1/ops/cloud-artifact-smoke/route';
const BACKEND_PATH = '/api/v1/ops/cloud-artifact-smoke';

function mockFetch(opts: { status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.status < 400,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

function getReq() {
  return new Request(`http://localhost${BACKEND_PATH}`);
}

function postReq(body?: unknown) {
  return new Request(`http://localhost${BACKEND_PATH}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
}

function calls() {
  return (global.fetch as ReturnType<typeof vi.fn>).mock.calls;
}

describe('/api/v1/ops/cloud-artifact-smoke proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('forwards GET to the backend and returns the smoke status', async () => {
    mockFetch({
      status: 200,
      data: { running: false, docker_available: true, coverage_gaps: ['EC2 provisioning'] },
    });

    const { GET } = await import(ROUTE);
    const response = (await GET(getReq())) as Response;

    const [calledUrl, calledOptions] = calls()[0];
    expect(calledUrl).toBe(`http://127.0.0.1:8000${BACKEND_PATH}`);
    expect(calledOptions.method).toBe('GET');
    // The panel polls this while a run is in flight; a cached body would show
    // a finished run as still running (or the reverse).
    expect(calledOptions.cache).toBe('no-store');

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
    expect(await response.json()).toEqual({
      running: false,
      docker_available: true,
      coverage_gaps: ['EC2 provisioning'],
    });
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ status: 200 });

    const { GET } = await import(ROUTE);
    await GET(getReq());

    expect(calls()[0][0]).toBe(`http://custom-backend:9000${BACKEND_PATH}`);
  });

  it('returns a structured 502 when the backend is unreachable on GET', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import(ROUTE);
    const response = (await GET(getReq())) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      error: 'Failed to fetch the cloud artifact smoke status from backend',
    });
  });

  it('forwards POST with the run options the panel chose', async () => {
    mockFetch({ status: 200, data: { started: true, started_at: '2026-08-15T02:00:00+00:00' } });

    const { POST } = await import(ROUTE);
    const response = (await POST(
      postReq({ version: '3.0.0rc9', inject: ['old-python'] })
    )) as Response;

    const [calledUrl, calledOptions] = calls()[0];
    expect(calledUrl).toBe(`http://127.0.0.1:8000${BACKEND_PATH}`);
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual({
      version: '3.0.0rc9',
      inject: ['old-python'],
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      started: true,
      started_at: '2026-08-15T02:00:00+00:00',
    });
  });

  it('starts a default run when the request carries no body', async () => {
    mockFetch({ status: 200, data: { started: true } });

    const { POST } = await import(ROUTE);
    await POST(postReq());

    // An unparseable/absent body is an empty options object, not a 500: the
    // backend's own defaults (latest published version, no fault) are exactly
    // what a plain "Run smoke" click means.
    expect(JSON.parse(calls()[0][1].body)).toEqual({});
  });

  it('starts a default run when the request body parses to null', async () => {
    mockFetch({ status: 200, data: { started: true } });

    const { POST } = await import(ROUTE);
    await POST(
      new Request(`http://localhost${BACKEND_PATH}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: 'null',
      })
    );

    // `null` parses fine, so the `.catch` never fires -- it still must not
    // reach the backend as a literal `null` payload.
    expect(JSON.parse(calls()[0][1].body)).toEqual({});
  });

  it('passes a refusal from the backend straight through', async () => {
    // The 409 the backend raises when a run is already in flight, in the API's
    // error envelope. Rewriting it here would cost the panel the only
    // explanation it can show the operator.
    const refusal = {
      error: {
        code: 'http_error',
        message: 'A containerized cloud smoke started at 2026-08-15T02:00:00+00:00 is still running',
      },
    };
    mockFetch({ status: 409, data: refusal });

    const { POST } = await import(ROUTE);
    const response = (await POST(postReq({}))) as Response;

    expect(response.status).toBe(409);
    expect(await response.json()).toEqual(refusal);
  });

  it('returns a structured 502 when the backend is unreachable on POST', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import(ROUTE);
    const response = (await POST(postReq({}))) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      error: 'Failed to start the cloud artifact smoke',
    });
  });

  it('returns a structured 502 when the backend body is not valid JSON', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: () => Promise.reject(new Error('invalid JSON')),
    });

    const { GET } = await import(ROUTE);
    const response = (await GET(getReq())) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      error: 'Failed to fetch the cloud artifact smoke status from backend',
    });
  });

  it('tags responses with a correlation id', async () => {
    mockFetch({ status: 200 });

    const { GET } = await import(ROUTE);
    const response = (await GET(getReq())) as Response;

    expect(response.headers.get('X-Request-Id')).toBeTruthy();
  });

  it('exposes only the two handlers the panel uses', async () => {
    const route = await import(ROUTE);

    expect(route.GET).toBeDefined();
    expect(route.POST).toBeDefined();
    expect(route.PUT).toBeUndefined();
    expect(route.PATCH).toBeUndefined();
    expect(route.DELETE).toBeUndefined();
  });
});
