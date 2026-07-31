import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// #3430: covers the Node.js server instrumentation hook (`register` /
// `onRequestError`) -- the tracing-enabled/disabled branches, the
// degrade-gracefully catch when the exporter setup throws, and the
// DSN-set/unset Sentry init branches (including `onRequestError`'s
// early-return when no DSN is configured).
const otelMocks = vi.hoisted(() => ({
  registerOTel: vi.fn(),
  exporterCtor: vi.fn(),
}));

vi.mock('@vercel/otel', () => ({
  registerOTel: (...args: unknown[]) => otelMocks.registerOTel(...args),
  OTLPHttpJsonTraceExporter: class {
    constructor(...args: unknown[]) {
      otelMocks.exporterCtor(...args);
    }
  },
}));

const sentryMocks = vi.hoisted(() => ({
  init: vi.fn(),
  captureRequestError: vi.fn(),
}));

vi.mock('@sentry/nextjs', () => ({
  init: (...args: unknown[]) => sentryMocks.init(...args),
  captureRequestError: (...args: unknown[]) => sentryMocks.captureRequestError(...args),
}));

describe('instrumentation (server hook)', () => {
  const ORIGINAL_ENV = { ...process.env };

  beforeEach(() => {
    vi.resetModules();
    otelMocks.registerOTel.mockReset();
    otelMocks.exporterCtor.mockReset();
    sentryMocks.init.mockReset();
    sentryMocks.captureRequestError.mockReset();
    process.env = { ...ORIGINAL_ENV, NEXT_RUNTIME: 'nodejs' };
    delete process.env.NYXGPT_TRACING_ENABLED;
    delete process.env.NYXGPT_OTLP_ENDPOINT;
    delete process.env.NYXGPT_ERROR_TRACKING_DSN;
    delete process.env.NYXGPT_ERROR_TRACKING_ENVIRONMENT;
  });

  afterEach(() => {
    process.env = ORIGINAL_ENV;
    vi.restoreAllMocks();
  });

  describe('register', () => {
    it('does nothing outside the Node.js runtime (e.g. edge)', async () => {
      process.env.NEXT_RUNTIME = 'edge';
      const { register } = await import('../src/instrumentation');

      await register();

      expect(otelMocks.registerOTel).not.toHaveBeenCalled();
    });

    it('registers OTel against a configured endpoint and logs the boot line', async () => {
      process.env.NYXGPT_OTLP_ENDPOINT = 'http://collector:4318/v1/traces';
      const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
      const { register } = await import('../src/instrumentation');

      await register();

      expect(otelMocks.registerOTel).toHaveBeenCalledWith(
        expect.objectContaining({ serviceName: 'nyxgpt-web' })
      );
      expect(otelMocks.exporterCtor).toHaveBeenCalledWith({
        url: 'http://collector:4318/v1/traces',
      });
      const line = logSpy.mock.calls[0][0] as string;
      expect(line).toContain(
        'distributed tracing enabled (otlp_endpoint=http://collector:4318/v1/traces)'
      );
    });

    it('registers OTel against the default local endpoint when unset', async () => {
      vi.spyOn(console, 'log').mockImplementation(() => {});
      const { register } = await import('../src/instrumentation');

      await register();

      expect(otelMocks.exporterCtor).toHaveBeenCalledWith({
        url: 'http://localhost:4318/v1/traces',
      });
    });

    it('skips OTel registration entirely when tracing is disabled', async () => {
      process.env.NYXGPT_TRACING_ENABLED = 'false';
      const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
      const { register } = await import('../src/instrumentation');

      await register();

      expect(otelMocks.registerOTel).not.toHaveBeenCalled();
      expect(logSpy).not.toHaveBeenCalled();
    });

    it('degrades gracefully and logs a warning when registerOTel throws', async () => {
      otelMocks.registerOTel.mockImplementation(() => {
        throw new Error('malformed endpoint');
      });
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      const { register } = await import('../src/instrumentation');

      await expect(register()).resolves.toBeUndefined();

      const line = warnSpy.mock.calls[0][0] as string;
      expect(line).toContain('failed to initialize distributed tracing: malformed endpoint');
    });

    it('initializes Sentry error tracking with the configured environment when a DSN is set', async () => {
      process.env.NYXGPT_ERROR_TRACKING_DSN = 'https://key@glitchtip.local/1';
      process.env.NYXGPT_ERROR_TRACKING_ENVIRONMENT = 'staging';
      vi.spyOn(console, 'log').mockImplementation(() => {});
      const { register } = await import('../src/instrumentation');

      await register();

      expect(sentryMocks.init).toHaveBeenCalledWith({
        dsn: 'https://key@glitchtip.local/1',
        environment: 'staging',
        tracesSampleRate: 0,
      });
    });

    it('defaults the Sentry environment to development when unset', async () => {
      process.env.NYXGPT_ERROR_TRACKING_DSN = 'https://key@glitchtip.local/1';
      vi.spyOn(console, 'log').mockImplementation(() => {});
      const { register } = await import('../src/instrumentation');

      await register();

      expect(sentryMocks.init).toHaveBeenCalledWith(
        expect.objectContaining({ environment: 'development' })
      );
    });

    it('does not initialize Sentry when no DSN is configured', async () => {
      vi.spyOn(console, 'log').mockImplementation(() => {});
      const { register } = await import('../src/instrumentation');

      await register();

      expect(sentryMocks.init).not.toHaveBeenCalled();
    });
  });

  describe('onRequestError', () => {
    it('no-ops when no error-tracking DSN is configured', async () => {
      const { onRequestError } = await import('../src/instrumentation');

      await onRequestError(
        new Error('boom'),
        { path: '/api/chat', method: 'GET', headers: {} },
        { routerKind: 'App Router', routePath: '/api/chat', routeType: 'route' }
      );

      expect(sentryMocks.captureRequestError).not.toHaveBeenCalled();
    });

    it('forwards the error to Sentry when a DSN is configured', async () => {
      process.env.NYXGPT_ERROR_TRACKING_DSN = 'https://key@glitchtip.local/1';
      const { onRequestError } = await import('../src/instrumentation');
      const error = new Error('boom');
      const request = { path: '/api/chat', method: 'GET', headers: {} };
      const context = { routerKind: 'App Router', routePath: '/api/chat', routeType: 'route' };

      await onRequestError(error, request, context);

      expect(sentryMocks.captureRequestError).toHaveBeenCalledWith(error, request, context);
    });
  });
});
