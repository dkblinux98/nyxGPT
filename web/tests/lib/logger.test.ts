import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

describe('logger', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('formats an info line matching nyxgpt.logging.DEFAULT_FMT (timestamp LEVEL [request_id] logger: message)', async () => {
    const spy = vi.spyOn(console, 'log').mockImplementation(() => {});
    const { logger } = await import('../../src/lib/logger');

    logger.info('hello world');

    expect(spy).toHaveBeenCalledTimes(1);
    const line = spy.mock.calls[0][0] as string;
    expect(line).toMatch(
      /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO \[-\] nyxgpt\.web: hello world$/
    );
  });

  it('uses console.error for logger.error and console.warn for logger.warn', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { logger } = await import('../../src/lib/logger');

    logger.error('bad thing happened');
    logger.warn('careful now');

    expect(errorSpy).toHaveBeenCalledTimes(1);
    expect((errorSpy.mock.calls[0][0] as string)).toContain('ERROR [-] nyxgpt.web: bad thing happened');
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect((warnSpy.mock.calls[0][0] as string)).toContain('WARN [-] nyxgpt.web: careful now');
  });

  it('appends the error message/stack after a colon when an error is passed', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { logger } = await import('../../src/lib/logger');

    logger.error('upstream failed', new Error('boom'));

    const line = spy.mock.calls[0][0] as string;
    expect(line).toContain('upstream failed: boom');
  });

  it('includes the ambient request id from requestContext when set', async () => {
    const spy = vi.spyOn(console, 'log').mockImplementation(() => {});
    const { logger } = await import('../../src/lib/logger');
    const { withRequestId } = await import('../../src/lib/requestContext');

    withRequestId('req-abc', () => logger.info('scoped message'));

    const line = spy.mock.calls[0][0] as string;
    expect(line).toContain('[req-abc] nyxgpt.web: scoped message');
  });

  it('omits a trace suffix when no span is active', async () => {
    const spy = vi.spyOn(console, 'log').mockImplementation(() => {});
    const { logger } = await import('../../src/lib/logger');

    logger.info('no trace here');

    const line = spy.mock.calls[0][0] as string;
    expect(line).not.toContain('trace_id=');
  });
});
