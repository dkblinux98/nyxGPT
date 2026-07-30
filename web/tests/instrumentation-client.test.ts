import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// #3430: covers the browser instrumentation hook -- module-level code that
// wires a WebTracerProvider + FetchInstrumentation and, separately, browser
// Sentry error tracking. Both blocks are gated on env vars read at import
// time, so every scenario needs `vi.resetModules()` + a fresh dynamic
// import, matching web/tests/lib/logger.test.ts's pattern for this module
// shape.
const otelMocks = vi.hoisted(() => ({
  providerCtor: vi.fn(),
  providerRegister: vi.fn(),
  batchSpanProcessorCtor: vi.fn(),
  exporterCtor: vi.fn(),
  resourceFromAttributes: vi.fn((attrs: unknown) => attrs),
  registerInstrumentations: vi.fn(),
  fetchInstrumentationCtor: vi.fn(),
}));

vi.mock('@opentelemetry/sdk-trace-web', () => ({
  WebTracerProvider: class {
    constructor(...args: unknown[]) {
      otelMocks.providerCtor(...args);
    }
    register(...args: unknown[]) {
      otelMocks.providerRegister(...args);
    }
  },
  BatchSpanProcessor: class {
    constructor(...args: unknown[]) {
      otelMocks.batchSpanProcessorCtor(...args);
    }
  },
}));

vi.mock('@opentelemetry/exporter-trace-otlp-http', () => ({
  OTLPTraceExporter: class {
    constructor(...args: unknown[]) {
      otelMocks.exporterCtor(...args);
    }
  },
}));

vi.mock('@opentelemetry/resources', () => ({
  resourceFromAttributes: (...args: unknown[]) => otelMocks.resourceFromAttributes(...args),
}));

vi.mock('@opentelemetry/instrumentation', () => ({
  registerInstrumentations: (...args: unknown[]) => otelMocks.registerInstrumentations(...args),
}));

vi.mock('@opentelemetry/instrumentation-fetch', () => ({
  FetchInstrumentation: class {
    constructor(...args: unknown[]) {
      otelMocks.fetchInstrumentationCtor(...args);
    }
  },
}));

const sentryMocks = vi.hoisted(() => ({
  init: vi.fn(),
}));

vi.mock('@sentry/nextjs', () => ({
  init: (...args: unknown[]) => sentryMocks.init(...args),
}));

describe('instrumentation-client (browser hook)', () => {
  const ORIGINAL_ENV = { ...process.env };

  beforeEach(() => {
    vi.resetModules();
    otelMocks.providerCtor.mockReset();
    otelMocks.providerRegister.mockReset();
    otelMocks.batchSpanProcessorCtor.mockReset();
    otelMocks.exporterCtor.mockReset();
    otelMocks.resourceFromAttributes.mockClear();
    otelMocks.registerInstrumentations.mockReset();
    otelMocks.fetchInstrumentationCtor.mockReset();
    sentryMocks.init.mockReset();
    process.env = { ...ORIGINAL_ENV };
    delete process.env.NEXT_PUBLIC_NYXGPT_TRACING_ENABLED;
    delete process.env.NEXT_PUBLIC_NYXGPT_OTLP_ENDPOINT;
    delete process.env.NEXT_PUBLIC_NYXGPT_ERROR_TRACKING_DSN;
    delete process.env.NEXT_PUBLIC_NYXGPT_ERROR_TRACKING_ENVIRONMENT;
  });

  afterEach(() => {
    process.env = ORIGINAL_ENV;
    vi.restoreAllMocks();
  });

  it('registers a WebTracerProvider against the default local endpoint by default', async () => {
    await import('../src/instrumentation-client');

    expect(otelMocks.providerCtor).toHaveBeenCalledTimes(1);
    expect(otelMocks.exporterCtor).toHaveBeenCalledWith({
      url: 'http://localhost:4318/v1/traces',
    });
    expect(otelMocks.providerRegister).toHaveBeenCalledTimes(1);
    expect(otelMocks.registerInstrumentations).toHaveBeenCalledTimes(1);
    expect(otelMocks.fetchInstrumentationCtor).toHaveBeenCalledTimes(1);
  });

  it('honors a custom OTLP endpoint', async () => {
    process.env.NEXT_PUBLIC_NYXGPT_OTLP_ENDPOINT = 'http://collector:4318/v1/traces';

    await import('../src/instrumentation-client');

    expect(otelMocks.exporterCtor).toHaveBeenCalledWith({
      url: 'http://collector:4318/v1/traces',
    });
  });

  it('skips browser tracing setup entirely when disabled', async () => {
    process.env.NEXT_PUBLIC_NYXGPT_TRACING_ENABLED = 'false';

    await import('../src/instrumentation-client');

    expect(otelMocks.providerCtor).not.toHaveBeenCalled();
    expect(otelMocks.registerInstrumentations).not.toHaveBeenCalled();
  });

  it('degrades gracefully and logs to the console when provider setup throws', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    otelMocks.providerRegister.mockImplementation(() => {
      throw new Error('collector unreachable');
    });

    await import('../src/instrumentation-client');

    expect(errorSpy).toHaveBeenCalledWith(
      expect.stringContaining('failed to initialize browser tracing (degrading gracefully):'),
      expect.any(Error)
    );
  });

  it('initializes Sentry error tracking with the configured environment when a DSN is set', async () => {
    process.env.NEXT_PUBLIC_NYXGPT_ERROR_TRACKING_DSN = 'https://key@glitchtip.local/1';
    process.env.NEXT_PUBLIC_NYXGPT_ERROR_TRACKING_ENVIRONMENT = 'staging';

    await import('../src/instrumentation-client');
    await vi.waitFor(() => expect(sentryMocks.init).toHaveBeenCalled());

    expect(sentryMocks.init).toHaveBeenCalledWith({
      dsn: 'https://key@glitchtip.local/1',
      environment: 'staging',
      tracesSampleRate: 0,
    });
  });

  it('defaults the Sentry environment to development when unset', async () => {
    process.env.NEXT_PUBLIC_NYXGPT_ERROR_TRACKING_DSN = 'https://key@glitchtip.local/1';

    await import('../src/instrumentation-client');
    await vi.waitFor(() => expect(sentryMocks.init).toHaveBeenCalled());

    expect(sentryMocks.init).toHaveBeenCalledWith(
      expect.objectContaining({ environment: 'development' })
    );
  });

  it('does not initialize Sentry when no DSN is configured', async () => {
    await import('../src/instrumentation-client');
    // Flush pending microtasks so a delayed init() can't hide behind timing.
    await Promise.resolve();
    await Promise.resolve();

    expect(sentryMocks.init).not.toHaveBeenCalled();
  });
});
