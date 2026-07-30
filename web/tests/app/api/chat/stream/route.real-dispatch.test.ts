/**
 * #3440 regression: exercises chat/stream/route.ts against a real local HTTP
 * server with NO mocking of `undici` or `fetch`. The pre-#3440 test suite
 * mocked `global.fetch` entirely, which hid the real bug — an incompatible
 * dispatcher/Agent thrown into Next's fetch — because a mocked fetch never
 * touches the real undici dispatch machinery. Here the route's actual
 * `undici.request()` call hits a genuine socket, so a future regression that
 * reintroduces an incompatible transport (e.g. passing a foreign-undici
 * dispatcher into `fetch()` again) has a real chance of failing this suite
 * instead of passing behind a mock.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createServer } from 'node:http';
import type { IncomingMessage, Server, ServerResponse } from 'node:http';
import type { AddressInfo } from 'node:net';

const ROUTE_PATH = '../../../../../src/app/api/chat/stream/route';

function makeRequest(body: string) {
  return new Request('http://localhost/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
  });
}

async function drainText(response: Response): Promise<string> {
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let out = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    out += decoder.decode(value, { stream: true });
  }
  return out;
}

describe('/api/chat/stream POST route — real undici dispatch (no mocks)', () => {
  let server: Server | undefined;
  let baseUrl = '';
  let received: { headers: IncomingMessage['headers']; body: string } | null = null;

  function startServer(handler: (req: IncomingMessage, res: ServerResponse) => void) {
    server = createServer(handler);
    return new Promise<void>((resolve) => {
      server!.listen(0, '127.0.0.1', () => {
        const { port } = server!.address() as AddressInfo;
        baseUrl = `http://127.0.0.1:${port}`;
        resolve();
      });
    });
  }

  beforeEach(() => {
    vi.resetModules();
    received = null;
    vi.spyOn(console, 'log').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(async () => {
    delete process.env.NYXGPT_API_BASE_URL;
    vi.restoreAllMocks();
    if (server) {
      await new Promise<void>((resolve) => server!.close(() => resolve()));
      server = undefined;
    }
  });

  it('streams a real SSE response end-to-end through the genuine undici transport', async () => {
    await startServer((req, res) => {
      let raw = '';
      req.on('data', (chunk) => {
        raw += chunk;
      });
      req.on('end', () => {
        received = { headers: req.headers, body: raw };
        res.writeHead(200, { 'Content-Type': 'text/event-stream' });
        res.write('data: hello\n\n');
        setTimeout(() => {
          res.write('data: world\n\n');
          res.end();
        }, 5);
      });
    });
    process.env.NYXGPT_API_BASE_URL = baseUrl;

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ session: 's1', prompt: 'hi' }));
    const response = (await POST(req as never)) as Response;

    expect(response.status).toBe(200);
    const text = await drainText(response);
    expect(text).toBe('data: hello\n\ndata: world\n\n');

    expect(received?.headers['x-client-supports-sse']).toBe('true');
    expect(JSON.parse(received!.body)).toEqual({ session: 's1', prompt: 'hi' });
  });

  it('passes through the real upstream status and body when the backend rejects the request', async () => {
    await startServer((_req, res) => {
      res.writeHead(503, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ detail: 'model runtime unavailable' }));
    });
    process.env.NYXGPT_API_BASE_URL = baseUrl;

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));
    const response = (await POST(req as never)) as Response;

    expect(response.status).toBe(503);
    const payload = await response.json();
    expect(payload.error).toBe('Upstream chat stream failed');
    expect(JSON.parse(payload.detail)).toEqual({ detail: 'model runtime unavailable' });
  });

  it('returns 502 when the real upstream connection is refused', async () => {
    // Nothing listens on this address — a real connection-refused error.
    process.env.NYXGPT_API_BASE_URL = 'http://127.0.0.1:1';

    const { POST } = await import(ROUTE_PATH);
    const req = makeRequest(JSON.stringify({ prompt: 'hi' }));
    const response = (await POST(req as never)) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: 'Upstream unreachable' });
  });
});
