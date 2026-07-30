import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

describe('withRequestLog', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('sets X-Request-Id on the response, reusing the client-supplied header', async () => {
    const { withRequestLog } = await import('../../src/lib/withRequestLog');
    const handler = withRequestLog(async () => new Response('ok'));

    const request = new Request('http://localhost/api/v1/models', {
      headers: { 'X-Request-Id': 'client-req-id' },
    });
    const response = await handler(request, undefined);

    expect(response.headers.get('X-Request-Id')).toBe('client-req-id');
  });

  it('generates an X-Request-Id when the client sent none', async () => {
    const { withRequestLog } = await import('../../src/lib/withRequestLog');
    const handler = withRequestLog(async () => new Response('ok'));

    const response = await handler(new Request('http://localhost/api/v1/models'), undefined);

    expect(response.headers.get('X-Request-Id')).toMatch(/^[0-9a-f-]{36}$/);
  });

  it('makes the request id available to logger calls inside the handler', async () => {
    const spy = vi.spyOn(console, 'log').mockImplementation(() => {});
    const { withRequestLog } = await import('../../src/lib/withRequestLog');
    const { logger } = await import('../../src/lib/logger');

    const handler = withRequestLog(async () => {
      logger.info('inside handler');
      return new Response('ok');
    });
    await handler(
      new Request('http://localhost/api/v1/models', { headers: { 'X-Request-Id': 'req-xyz' } }),
      undefined
    );

    const line = spy.mock.calls[0][0] as string;
    expect(line).toContain('[req-xyz] nyxgpt.web: inside handler');
  });

  it('logs and re-throws when the wrapped handler throws', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { withRequestLog } = await import('../../src/lib/withRequestLog');

    const handler = withRequestLog(async () => {
      throw new Error('handler exploded');
    });

    await expect(
      handler(new Request('http://localhost/api/v1/models'), undefined)
    ).rejects.toThrow('handler exploded');
    expect(errorSpy).toHaveBeenCalledTimes(1);
    expect(errorSpy.mock.calls[0][0] as string).toContain('Unhandled error in GET /api/v1/models');
  });

  it('does not throw when invoked with no request (legacy zero-arg handler call)', async () => {
    const { withRequestLog } = await import('../../src/lib/withRequestLog');
    const handler = withRequestLog(async () => new Response('ok'));

    // Mirrors how some pre-#3430 route tests call the exported handler
    // directly with no arguments; Next.js itself always passes a Request.
    const response = await (handler as (req?: Request) => Promise<Response>)();

    expect(response.headers.get('X-Request-Id')).toMatch(/^[0-9a-f-]{36}$/);
  });
});
