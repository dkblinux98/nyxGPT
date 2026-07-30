import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

describe('logger', () => {
  const originalLogDir = process.env.NYXGPT_LOG_DIR;

  beforeEach(() => {
    vi.resetModules();
    delete process.env.NYXGPT_LOG_DIR;
  });

  afterEach(() => {
    if (originalLogDir === undefined) delete process.env.NYXGPT_LOG_DIR;
    else process.env.NYXGPT_LOG_DIR = originalLogDir;
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

  it('matches the promtail regex nyxgpt.logging.DEFAULT_FMT lines match', async () => {
    // Mirrors tests/unit/test_log_aggregation_stack.py's promtail contract
    // check on the Python side (#3430) -- the two loggers must produce
    // interchangeable lines for the same regex to extract level/logger from
    // either tier's output.
    const promtailRegex =
      /^(?<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,\d{3})? (?<level>\w+) (?:\[(?<request_id>[^\]]*)\] )?(?<logger>\S+): (?<message>.*)$/;
    const spy = vi.spyOn(console, 'log').mockImplementation(() => {});
    const { logger } = await import('../../src/lib/logger');

    logger.info('handling request');

    const line = spy.mock.calls[0][0] as string;
    const match = line.match(promtailRegex);
    expect(match).not.toBeNull();
    expect(match?.groups?.level).toBe('INFO');
    expect(match?.groups?.logger).toBe('nyxgpt.web');
    expect(match?.groups?.message).toBe('handling request');
  });

  it('appends to NYXGPT_LOG_DIR/nyxgpt-web.log when NYXGPT_LOG_DIR is set (Compose-mode log shipping)', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'nyxgpt-web-log-'));
    process.env.NYXGPT_LOG_DIR = dir;
    vi.spyOn(console, 'log').mockImplementation(() => {});

    try {
      const { logger } = await import('../../src/lib/logger');
      logger.info('shipped to file');

      const contents = readFileSync(join(dir, 'nyxgpt-web.log'), 'utf-8');
      expect(contents).toContain('nyxgpt.web: shipped to file');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('does not write a file when NYXGPT_LOG_DIR is unset', async () => {
    vi.spyOn(console, 'log').mockImplementation(() => {});
    const { logger } = await import('../../src/lib/logger');

    // Just asserts this doesn't throw and stays console-only -- there's no
    // file path to check when NYXGPT_LOG_DIR is unset by design.
    expect(() => logger.info('console only')).not.toThrow();
  });
});
