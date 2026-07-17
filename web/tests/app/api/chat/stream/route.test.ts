/**
 * Tests for the /api/chat/stream Next.js proxy route.
 *
 * Issue #3245: raise web/ vitest coverage to 100% with real behavioral
 * tests. This route had zero coverage. It:
 *  - parses the request JSON body (400 on invalid JSON)
 *  - forwards to the backend chat stream endpoint via apiFetch()
 *  - returns 502 when the upstream fetch throws
 *  - returns 502 with a `detail` string when the upstream response is not
 *    ok or has no body (and swallows a throwing upstream.text())
 *  - otherwise re-streams upstream.body chunk by chunk through a wrapping
 *    ReadableStream, including pull()/cancel() error handling.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { Agent } from 'undici';

const ROUTE_PATH = '../../../../../src/app/api/chat/stream/route';

type MockUpstreamReader = {
  read: ReturnType<typeof vi.fn>;
  cancel: ReturnType<typeof vi.fn>;
};

function makeReaderFromChunks(chunks: Uint8Array[]): MockUpstreamReader {
  let i = 0;
  const read = vi.fn(async () => {
    if (i < chunks.length) {
      return { done: false, value: chunks[i++] };
    }
    return { done: true, value: undefined };
  });
  const cancel = vi.fn().mockResolvedValue(undefined);
  return { read, cancel };
}

function mockUpstream(overrides: {
  ok: boolean;
  status: number;
  body?: unknown;
  text?: ReturnType<typeof vi.fn>;
}) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: overrides.ok,
    status: overrides.status,
    body: overrides.body ?? null,
    text: overrides.text ?? vi.fn().mockResolvedValue(''),
    headers: new Headers(),
  });
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
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
    delete process.env.NYXGPT_AUTH_API_KEY;
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
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('returns 502 "Upstream unreachable" when apiFetch throws', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ session: 's1', prompt: 'hi' }));

    const response = (await POST(req as never)) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: 'Upstream unreachable' });
  });

  it('returns 502 with detail text when upstream response is not ok', async () => {
    mockUpstream({
      ok: false,
      status: 500,
      body: null,
      text: vi.fn().mockResolvedValue('backend exploded'),
    });

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));

    const response = (await POST(req as never)) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      error: 'Upstream chat stream failed',
      detail: 'backend exploded',
    });
  });

  it('returns 502 when upstream response is ok but has no body', async () => {
    mockUpstream({
      ok: true,
      status: 200,
      body: null,
      text: vi.fn().mockResolvedValue(''),
    });

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));

    const response = (await POST(req as never)) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      error: 'Upstream chat stream failed',
      detail: '',
    });
  });

  it('returns 502 with empty detail when upstream.text() itself throws', async () => {
    mockUpstream({
      ok: false,
      status: 500,
      body: null,
      text: vi.fn().mockRejectedValue(new Error('text() blew up')),
    });

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));

    const response = (await POST(req as never)) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      error: 'Upstream chat stream failed',
      detail: '',
    });
  });

  it('streams upstream body chunks through to the client byte-for-byte', async () => {
    const encoder = new TextEncoder();
    const chunk1 = encoder.encode('data: hello\n\n');
    const chunk2 = encoder.encode('data: world\n\n');
    const mockReader = makeReaderFromChunks([chunk1, chunk2]);

    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: { getReader: () => mockReader },
      text: vi.fn(),
      headers: new Headers(),
    });

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
    expect(mockReader.read).toHaveBeenCalledTimes(3); // 2 chunks + final done
  });

  it('tolerates a read() result with done:false and an empty value', async () => {
    // Defensive branch: `if (value)` inside pull() guards against a reader
    // that reports not-done with no value.
    let call = 0;
    const mockReader: MockUpstreamReader = {
      read: vi.fn(async () => {
        call += 1;
        if (call === 1) {
          return { done: false, value: undefined };
        }
        return { done: true, value: undefined };
      }),
      cancel: vi.fn().mockResolvedValue(undefined),
    };

    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: { getReader: () => mockReader },
      text: vi.fn(),
      headers: new Headers(),
    });

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));

    const response = (await POST(req as never)) as Response;
    const bytes = await drainStream(response);

    expect(bytes.byteLength).toBe(0);
  });

  it('propagates a read error from pull() to the response stream', async () => {
    const readError = new Error('upstream read boom');
    const mockReader: MockUpstreamReader = {
      read: vi.fn().mockRejectedValue(readError),
      cancel: vi.fn().mockResolvedValue(undefined),
    };

    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: { getReader: () => mockReader },
      text: vi.fn(),
      headers: new Headers(),
    });

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));

    const response = (await POST(req as never)) as Response;
    const reader = response.body!.getReader();

    await expect(reader.read()).rejects.toThrow('upstream read boom');
  });

  it('cancels the upstream reader when the client cancels the response stream', async () => {
    const mockReader: MockUpstreamReader = {
      // Never resolves on its own; we cancel before it would.
      read: vi.fn(() => new Promise(() => {})),
      cancel: vi.fn().mockResolvedValue(undefined),
    };

    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      body: { getReader: () => mockReader },
      text: vi.fn(),
      headers: new Headers(),
    });

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));

    const response = (await POST(req as never)) as Response;
    const reader = response.body!.getReader();
    await reader.cancel('client went away');

    expect(mockReader.cancel).toHaveBeenCalledTimes(1);
  });

  it('forwards the JSON body and SSE headers to the upstream fetch call', async () => {
    mockUpstream({
      ok: true,
      status: 200,
      body: null,
      text: vi.fn().mockResolvedValue(''),
    });

    const { POST } = await import(ROUTE_PATH);
    const payload = {
      session: 's1',
      prompt: 'hello there',
      model: 'gpt-test',
      rag_enabled: true,
    };
    const req = makeRequest(JSON.stringify(payload));

    await POST(req as never);

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOptions] = (
      global.fetch as ReturnType<typeof vi.fn>
    ).mock.calls[0];

    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/chat/stream');
    expect(calledOptions.method).toBe('POST');
    expect(JSON.parse(calledOptions.body)).toEqual(payload);
    expect(calledOptions.cache).toBe('no-store');
    expect(calledOptions.dispatcher).toBeInstanceOf(Agent);

    const headers = calledOptions.headers as Headers;
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(headers.get('Accept')).toBe('text/event-stream');
    expect(headers.get('X-Client-Supports-SSE')).toBe('true');
    expect(headers.get('X-Client-Supports-Structured-Events')).toBe('true');
    expect(headers.get('X-Client-Supports-Streaming')).toBe('true');
    expect(headers.get('X-Client-Version')).toBe('web-ui/1.0.0');
    expect(headers.get('X-Client-Max-Event-Size')).toBe('0');
  });

  it('uses NYXGPT_API_BASE_URL when set to build the upstream URL', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockUpstream({
      ok: true,
      status: 200,
      body: null,
      text: vi.fn().mockResolvedValue(''),
    });

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));
    await POST(req as never);

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock
      .calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/chat/stream');
  });
});
