/**
 * Tests for the /api/chat/stream Next.js proxy route.
 *
 * #3440: this route used to drive its upstream call through Next's built-in
 * `fetch` with a `dispatcher` built from the `undici` package pinned in
 * package.json. Next 16 bundles its own (newer-major) undici for global
 * fetch, so that dispatcher was rejected with `UND_ERR_INVALID_ARG` before
 * any network I/O — every chat request 502'd instantly. The route now
 * drives the upstream call through undici's own `request()` instead, so
 * these tests mock the `undici` module's `request` export directly (not
 * `global.fetch`, which this route no longer calls at all).
 *
 * See route.real-dispatch.test.ts for a companion suite that exercises the
 * real (unmocked) undici transport against a real local HTTP server — the
 * kind of test that would have caught the original dispatcher bug, since
 * mocking `global.fetch` never touched the real dispatch machinery.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const ROUTE_PATH = '../../../../../src/app/api/chat/stream/route';

vi.mock('undici', async () => {
  const actual = await vi.importActual<typeof import('undici')>('undici');
  return {
    ...actual,
    request: vi.fn(),
  };
});

import { request as undiciRequest } from 'undici';

const mockedRequest = vi.mocked(undiciRequest);

type Step = { done: boolean; value?: Uint8Array };

function makeBody(
  steps: Step[],
  overrides: { text?: () => Promise<string>; destroy?: () => void } = {}
) {
  let i = 0;
  const next = vi.fn(async () => {
    if (i < steps.length) return steps[i++];
    return { done: true, value: undefined };
  });
  const destroy = overrides.destroy ?? vi.fn();
  return {
    [Symbol.asyncIterator]() {
      return { next };
    },
    text: overrides.text ?? vi.fn().mockResolvedValue(''),
    destroy,
    __next: next,
  };
}

function mockUpstream(statusCode: number, body: ReturnType<typeof makeBody>) {
  mockedRequest.mockResolvedValueOnce({
    statusCode,
    headers: {},
    body: body as never,
    trailers: {},
    opaque: undefined,
    context: {},
  } as never);
}

function makeRequest(body: string) {
  return new Request('http://localhost/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
  });
}

async function drainStream(response: Response): Promise<Uint8Array> {
  const reader = response.body!.getReader();
  const chunks: Uint8Array[] = [];
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (value) chunks.push(value);
  }
  const total = chunks.reduce((n, c) => n + c.byteLength, 0);
  const combined = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) {
    combined.set(c, offset);
    offset += c.byteLength;
  }
  return combined;
}

describe('/api/chat/stream POST route', () => {
  beforeEach(() => {
    mockedRequest.mockReset();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
    delete process.env.NYXGPT_AUTH_API_KEY;
    delete process.env.NYXGPT_CHAT_STREAM_HEADERS_TIMEOUT_MS;
    vi.spyOn(console, 'log').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns 400 with "Invalid JSON" when the request body cannot be parsed', async () => {
    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest('{not valid json');

    const response = (await POST(req as never)) as Response;

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: 'Invalid JSON' });
    // Must fail before ever attempting to reach the backend.
    expect(mockedRequest).not.toHaveBeenCalled();
  });

  it('handles a request body with no prompt field (logs prompt_len as empty)', async () => {
    mockedRequest.mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ session: 's1' }));

    const response = (await POST(req as never)) as Response;

    expect(response.status).toBe(502);
  });

  it('returns 502 "Upstream unreachable" when the upstream request throws', async () => {
    mockedRequest.mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ session: 's1', prompt: 'hi' }));

    const response = (await POST(req as never)) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: 'Upstream unreachable' });
  });

  it('passes through the real upstream status and detail text when the response is not 2xx', async () => {
    mockUpstream(500, makeBody([], { text: vi.fn().mockResolvedValue('backend exploded') }));

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));

    const response = (await POST(req as never)) as Response;

    // No more flattening to a generic 502 — the real status reaches the browser.
    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({
      error: 'Upstream chat stream failed',
      detail: 'backend exploded',
    });
  });

  it('returns the real upstream status with empty detail when body.text() itself throws', async () => {
    mockUpstream(
      503,
      makeBody([], { text: vi.fn().mockRejectedValue(new Error('text() blew up')) })
    );

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));

    const response = (await POST(req as never)) as Response;

    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      error: 'Upstream chat stream failed',
      detail: '',
    });
  });

  it('streams upstream body chunks through to the client byte-for-byte', async () => {
    const encoder = new TextEncoder();
    const chunk1 = encoder.encode('data: hello\n\n');
    const chunk2 = encoder.encode('data: world\n\n');
    const body = makeBody([
      { done: false, value: chunk1 },
      { done: false, value: chunk2 },
    ]);
    mockUpstream(200, body);

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ session: 's1', prompt: 'hi' }));

    const response = (await POST(req as never)) as Response;

    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe(
      'text/event-stream; charset=utf-8'
    );
    expect(response.headers.get('Cache-Control')).toBe(
      'no-cache, no-transform, no-store'
    );
    expect(response.headers.get('Connection')).toBe('keep-alive');
    expect(response.headers.get('X-Accel-Buffering')).toBe('no');

    const bytes = await drainStream(response);
    expect(new TextDecoder().decode(bytes)).toBe(
      'data: hello\n\ndata: world\n\n'
    );
    expect(body.__next).toHaveBeenCalledTimes(3); // 2 chunks + final done
  });

  it('tolerates a next() result with done:false and an empty value', async () => {
    // Defensive branch: `if (value)` inside pull() guards against an
    // iterator that reports not-done with no value.
    const body = makeBody([{ done: false, value: undefined }]);
    mockUpstream(200, body);

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));

    const response = (await POST(req as never)) as Response;
    const bytes = await drainStream(response);

    expect(bytes.byteLength).toBe(0);
  });

  it('propagates a read error from pull() to the response stream', async () => {
    const readError = new Error('upstream read boom');
    const body = makeBody([]);
    body.__next.mockRejectedValue(readError);
    mockUpstream(200, body);

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));

    const response = (await POST(req as never)) as Response;
    const reader = response.body!.getReader();

    // pull() best-effort-enqueues a "[stream error]" marker chunk before
    // erroring the controller, so the first read() surfaces that marker...
    const first = await reader.read();
    expect(first.done).toBe(false);
    expect(new TextDecoder().decode(first.value)).toBe('\n[stream error]\n');

    // ...and only the next read() observes the controller error.
    await expect(reader.read()).rejects.toThrow('upstream read boom');
  });

  it('destroys the upstream body when the client cancels the response stream', async () => {
    const destroy = vi.fn();
    const body = makeBody([], { destroy });
    // Never resolves on its own; we cancel before it would.
    body.__next.mockImplementation(() => new Promise(() => {}));
    mockUpstream(200, body);

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));

    const response = (await POST(req as never)) as Response;
    const reader = response.body!.getReader();
    await reader.cancel('client went away');

    expect(destroy).toHaveBeenCalledTimes(1);
  });

  it('forwards the JSON body, SSE headers, auth key, and timeout options to undici request()', async () => {
    process.env.NYXGPT_AUTH_API_KEY = 'secret-key';
    mockUpstream(200, makeBody([]));

    const { POST } = await import(ROUTE_PATH);
    const payload = {
      session: 's1',
      prompt: 'hello there',
      model: 'gpt-test',
      rag_enabled: true,
    };
    const req = makeRequest(JSON.stringify(payload));

    await POST(req as never);

    expect(mockedRequest).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = mockedRequest.mock.calls[0];

    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/chat/stream');
    expect(calledOptions?.method).toBe('POST');
    expect(JSON.parse(calledOptions?.body as string)).toEqual(payload);
    expect(calledOptions?.headersTimeout).toBe(300_000);
    expect(calledOptions?.bodyTimeout).toBe(0);
    // No dispatcher/Agent — this route no longer feeds a foreign undici
    // Agent into anything (see #3440).
    expect(calledOptions).not.toHaveProperty('dispatcher');

    // Object.fromEntries(new Headers(...).entries()) preserves whatever
    // casing this runtime's Headers implementation normalizes to — do a
    // case-insensitive lookup rather than coupling the assertion to it.
    const headers = new Headers(calledOptions?.headers as HeadersInit);
    expect(headers.get('content-type')).toBe('application/json');
    expect(headers.get('accept')).toBe('text/event-stream');
    expect(headers.get('x-client-supports-sse')).toBe('true');
    expect(headers.get('x-client-supports-structured-events')).toBe('true');
    expect(headers.get('x-client-supports-streaming')).toBe('true');
    expect(headers.get('x-client-version')).toBe('web-ui/1.0.0');
    expect(headers.get('x-client-max-event-size')).toBe('0');
    expect(headers.get('x-api-key')).toBe('secret-key');
  });

  it('honors NYXGPT_CHAT_STREAM_HEADERS_TIMEOUT_MS to accommodate cold model loads', async () => {
    process.env.NYXGPT_CHAT_STREAM_HEADERS_TIMEOUT_MS = '900000';
    mockUpstream(200, makeBody([]));

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));
    await POST(req as never);

    const [, calledOptions] = mockedRequest.mock.calls[0];
    expect(calledOptions?.headersTimeout).toBe(900_000);
  });

  it('falls back to Date.now() when performance.now is unavailable', async () => {
    mockUpstream(200, makeBody([]));
    const originalNow = performance.now;
    // Shadow the prototype's now() with a non-function own property to
    // simulate a runtime where performance exists but lacks now().
    Object.defineProperty(performance, 'now', { value: undefined, configurable: true });

    try {
      const { POST } = await import(ROUTE_PATH);
      const req = makeRequest(JSON.stringify({ prompt: 'hi' }));
      const response = (await POST(req as never)) as Response;

      expect(response.status).toBe(200);
      await drainStream(response);
    } finally {
      Object.defineProperty(performance, 'now', { value: originalNow, configurable: true });
    }
  });

  it('uses NYXGPT_API_BASE_URL when set to build the upstream URL', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockUpstream(200, makeBody([]));

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));
    await POST(req as never);

    const [calledUrl] = mockedRequest.mock.calls[0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/chat/stream');
  });
});
