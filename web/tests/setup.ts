import '@testing-library/jest-dom';
import { beforeAll, afterEach, afterAll } from 'vitest';
import { cleanup } from '@testing-library/react';
import { server } from './mocks/server';
import { context, type Context, type ContextManager, ROOT_CONTEXT } from '@opentelemetry/api';

// The default OTel context API is a no-op unless a real ContextManager is
// registered (Node's SDK normally does this via AsyncHooksContextManager) --
// without it, `context.with(...)` never actually makes the given context
// active, so trace-context tests (logger.ts's trace_id/span_id suffix,
// requestContext.ts's trace-derived request id, #3430) would silently no-op.
// This minimal stack-based manager is enough for synchronous `with()` calls.
class StackContextManager implements ContextManager {
  private stack: Context[] = [ROOT_CONTEXT];

  active(): Context {
    return this.stack[this.stack.length - 1];
  }

  with<A extends unknown[], F extends (...args: A) => ReturnType<F>>(
    ctx: Context,
    fn: F,
    thisArg?: ThisParameterType<F>,
    ...args: A
  ): ReturnType<F> {
    this.stack.push(ctx);
    try {
      return fn.apply(thisArg, args);
    } finally {
      this.stack.pop();
    }
  }

  bind<T>(_ctx: Context, target: T): T {
    return target;
  }

  enable(): this {
    return this;
  }

  disable(): this {
    return this;
  }
}

context.setGlobalContextManager(new StackContextManager());

// Node >= 22 defines its own global `localStorage`/`sessionStorage`: getters
// that return undefined unless node runs with `--localstorage-file`
// (ExperimentalWarning: "localStorage is not available because
// --localstorage-file was not provided"). Vitest's happy-dom environment does
// not displace that pre-existing global, so on modern node every test touching
// `localStorage` got node's undefined instead of a working Storage -- 182
// failures, all "Cannot read properties of undefined (reading ...)". Replace
// the stub with a real happy-dom Storage as a plain writable property, so the
// suite behaves identically on every node version (CI pins node 20, where the
// stub doesn't exist) and tests can still assign their own mocks. Note
// `window === globalThis` under vitest globals, so this cannot be a getter
// deferring to `window.localStorage` -- that recurses into itself.
const storageWindow = new (await import('happy-dom')).Window();
for (const key of ['localStorage', 'sessionStorage'] as const) {
  delete (globalThis as Record<string, unknown>)[key];
  Object.defineProperty(globalThis, key, {
    configurable: true,
    writable: true,
    value: storageWindow[key],
  });
}

// Establish API mocking before all tests
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));

// Reset any request handlers that we may add during the tests,
// so they don't affect other tests
afterEach(() => {
  cleanup();
  server.resetHandlers();
});

// Clean up after the tests are finished
afterAll(() => server.close());

// Mock window.matchMedia
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(), // deprecated
    removeListener: vi.fn(), // deprecated
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

// Mock window.URL.createObjectURL and revokeObjectURL
Object.defineProperty(window.URL, 'createObjectURL', {
  writable: true,
  value: vi.fn(() => 'mock-blob-url'),
});

Object.defineProperty(window.URL, 'revokeObjectURL', {
  writable: true,
  value: vi.fn(),
});
