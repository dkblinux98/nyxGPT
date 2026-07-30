import { describe, it, expect, vi, beforeEach } from 'vitest';

describe('getRequestId / withRequestId', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('returns undefined outside any request context', async () => {
    const { getRequestId } = await import('../../src/lib/requestContext');
    expect(getRequestId()).toBeUndefined();
  });

  it('returns the id set by withRequestId while inside its callback', async () => {
    const { getRequestId, withRequestId } = await import('../../src/lib/requestContext');
    const observed = withRequestId('req-123', () => getRequestId());
    expect(observed).toBe('req-123');
  });

  it('propagates across awaited async work inside withRequestId', async () => {
    const { getRequestId, withRequestId } = await import('../../src/lib/requestContext');
    const observed = await withRequestId('req-async', async () => {
      await Promise.resolve();
      return getRequestId();
    });
    expect(observed).toBe('req-async');
  });

  it('is undefined again once withRequestId returns', async () => {
    const { getRequestId, withRequestId } = await import('../../src/lib/requestContext');
    withRequestId('req-scoped', () => getRequestId());
    expect(getRequestId()).toBeUndefined();
  });
});

describe('resolveRequestId', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('reuses the client-supplied X-Request-Id header', async () => {
    const { resolveRequestId } = await import('../../src/lib/requestContext');
    const request = new Request('http://localhost/api/v1/models', {
      headers: { 'X-Request-Id': 'client-supplied' },
    });
    expect(resolveRequestId(request)).toBe('client-supplied');
  });

  it('generates a fresh id when no header is present and no trace is active', async () => {
    const { resolveRequestId } = await import('../../src/lib/requestContext');
    const request = new Request('http://localhost/api/v1/models');
    const id = resolveRequestId(request);
    expect(id).toMatch(/^[0-9a-f-]{36}$/);
  });

  it('does not throw when called with no request (defensive fallback)', async () => {
    const { resolveRequestId } = await import('../../src/lib/requestContext');
    expect(() => resolveRequestId()).not.toThrow();
    expect(resolveRequestId()).toMatch(/^[0-9a-f-]{36}$/);
  });
});
