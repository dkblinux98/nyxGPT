import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

describe('apiUrl', () => {
  beforeEach(() => {
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('prepends a leading slash to a path that is missing one', async () => {
    const { apiUrl } = await import('../../src/lib/apiProxy');
    expect(apiUrl('api/v1/models')).toBe('http://127.0.0.1:8000/api/v1/models');
  });
});

describe('apiFetch', () => {
  const originalBase = process.env.NYXGPT_API_BASE_URL;
  const originalKey = process.env.NYXGPT_AUTH_API_KEY;

  beforeEach(() => {
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
    delete process.env.NYXGPT_AUTH_API_KEY;
  });

  afterEach(() => {
    if (originalBase === undefined) delete process.env.NYXGPT_API_BASE_URL;
    else process.env.NYXGPT_API_BASE_URL = originalBase;
    if (originalKey === undefined) delete process.env.NYXGPT_AUTH_API_KEY;
    else process.env.NYXGPT_AUTH_API_KEY = originalKey;
  });

  it('resolves against the default base URL when NYXGPT_API_BASE_URL is unset', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({ ok: true, status: 200 });
    const { apiFetch } = await import('../../src/lib/apiProxy');
    await apiFetch('/api/v1/models');
    const [url] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe('http://127.0.0.1:8000/api/v1/models');
  });

  it('resolves against NYXGPT_API_BASE_URL when set, stripping a trailing slash', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://api:8000/';
    global.fetch = vi.fn().mockResolvedValueOnce({ ok: true, status: 200 });
    const { apiFetch } = await import('../../src/lib/apiProxy');
    await apiFetch('/api/v1/models');
    const [url] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe('http://api:8000/api/v1/models');
  });

  it('attaches X-API-Key when NYXGPT_AUTH_API_KEY is set', async () => {
    process.env.NYXGPT_AUTH_API_KEY = 'secret-key';
    global.fetch = vi.fn().mockResolvedValueOnce({ ok: true, status: 200 });
    const { apiFetch } = await import('../../src/lib/apiProxy');
    await apiFetch('/api/v1/models');
    const [, init] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    const headers = init.headers as Headers;
    expect(headers.get('X-API-Key')).toBe('secret-key');
  });

  it('does not attach X-API-Key when NYXGPT_AUTH_API_KEY is unset', async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({ ok: true, status: 200 });
    const { apiFetch } = await import('../../src/lib/apiProxy');
    await apiFetch('/api/v1/models');
    const [, init] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    const headers = init.headers as Headers;
    expect(headers.has('X-API-Key')).toBe(false);
  });

  it('preserves caller-supplied headers alongside the auth header', async () => {
    process.env.NYXGPT_AUTH_API_KEY = 'secret-key';
    global.fetch = vi.fn().mockResolvedValueOnce({ ok: true, status: 200 });
    const { apiFetch } = await import('../../src/lib/apiProxy');
    await apiFetch('/api/v1/models', { headers: { 'Content-Type': 'application/json' } });
    const [, init] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    const headers = init.headers as Headers;
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(headers.get('X-API-Key')).toBe('secret-key');
  });
});
