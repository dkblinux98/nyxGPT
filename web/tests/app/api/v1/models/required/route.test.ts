/**
 * Tests for the /api/v1/models/required Next.js proxy route (#3824).
 *
 * This is the browser-facing half of the model-readiness surface: the panel
 * on the admin health page reads it, so a backend that cannot be reached has
 * to surface as a structured 502 rather than an unhandled throw.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const READY = {
  base_url: 'http://127.0.0.1:11434',
  reachable: true,
  error: '',
  ready: true,
  remediation: '',
  models: [
    { role: 'chat', model: 'qwen3:0.6b', setting: '[nyxgpt] default_model', present: true },
    {
      role: 'embedding',
      model: 'nomic-embed-text',
      setting: '[rag] embedding_model',
      present: true,
    },
  ],
};

function mockFetch(opts: { ok: boolean; status: number; data?: unknown }) {
  global.fetch = vi.fn().mockResolvedValueOnce({
    ok: opts.ok,
    status: opts.status,
    json: () => Promise.resolve(opts.data ?? {}),
  });
}

describe('/api/v1/models/required proxy route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    delete process.env.NYXGPT_API_BASE_URL;
  });

  it('passes the backend readiness payload through unchanged', async () => {
    mockFetch({ ok: true, status: 200, data: READY });

    const { GET } = await import('../../../../../../src/app/api/v1/models/required/route');
    const response = (await GET()) as Response;

    expect(global.fetch).toHaveBeenCalledTimes(1);
    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://127.0.0.1:8000/api/v1/models/required');
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(READY);
  });

  it('uses NYXGPT_API_BASE_URL when set', async () => {
    process.env.NYXGPT_API_BASE_URL = 'http://custom-backend:9000';
    mockFetch({ ok: true, status: 200, data: READY });

    const { GET } = await import('../../../../../../src/app/api/v1/models/required/route');
    await GET();

    const calledUrl: string = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(calledUrl).toBe('http://custom-backend:9000/api/v1/models/required');
  });

  it('passes through a non-2xx backend response status', async () => {
    mockFetch({ ok: false, status: 503, data: { detail: 'ollama unreachable' } });

    const { GET } = await import('../../../../../../src/app/api/v1/models/required/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ detail: 'ollama unreachable' });
  });

  it('returns 502 with structured error when the backend is unreachable', async () => {
    global.fetch = vi.fn().mockRejectedValueOnce(new Error('ECONNREFUSED'));

    const { GET } = await import('../../../../../../src/app/api/v1/models/required/route');
    const response = (await GET()) as Response;

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({
      error: 'Failed to fetch required-model readiness from backend',
    });
  });
});
