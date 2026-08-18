// Model-readiness panel (#3824): the dashboard surface for "can this stack
// answer a first chat message". A stack whose services all report healthy can
// still fail that message when Ollama holds no chat model, so the panel has to
// distinguish present / missing / unknown and offer the pull for the missing.

import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import RequiredModelsPanel from '../../src/components/RequiredModelsPanel';

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

const MISSING_EMBEDDING = {
  ...READY,
  ready: false,
  remediation:
    "Ollama is missing required model(s): 'nomic-embed-text' (embedding). " +
    'Re-run `nyxgpt ops install` (it pulls them), or pull directly: ' +
    'nyxgpt models pull nomic-embed-text.',
  models: [
    READY.models[0],
    { ...READY.models[1], present: false },
  ],
};

function mockFetch(responses: Array<{ ok?: boolean; json: unknown }>) {
  const fn = vi.fn();
  for (const r of responses) {
    fn.mockResolvedValueOnce({ ok: r.ok ?? true, json: async () => r.json });
  }
  global.fetch = fn as unknown as typeof fetch;
  return fn;
}

describe('RequiredModelsPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('lists both required models with their configured source', async () => {
    mockFetch([{ json: READY }]);
    render(<RequiredModelsPanel />);

    expect(await screen.findByText('qwen3:0.6b')).toBeInTheDocument();
    expect(screen.getByText('nomic-embed-text')).toBeInTheDocument();
    expect(screen.getByText(/\[nyxgpt\] default_model/)).toBeInTheDocument();
    expect(screen.getByText(/\[rag\] embedding_model/)).toBeInTheDocument();
    expect(screen.getAllByText('present')).toHaveLength(2);
  });

  it('offers a pull only for the missing model, and re-reads readiness after it', async () => {
    const fetchMock = mockFetch([
      { json: MISSING_EMBEDDING },
      { json: { ok: true, model: 'nomic-embed-text' } },
      { json: READY },
    ]);
    render(<RequiredModelsPanel />);

    const button = await screen.findByRole('button', { name: 'Pull nomic-embed-text' });
    expect(screen.queryByRole('button', { name: 'Pull qwen3:0.6b' })).not.toBeInTheDocument();
    // The remediation names nyxgpt commands, never a raw `ollama pull`.
    expect(screen.getByText(/nyxgpt ops install/)).toBeInTheDocument();
    expect(screen.queryByText(/ollama pull/)).not.toBeInTheDocument();

    fireEvent.click(button);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock.mock.calls[1][0]).toBe('/api/models');
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ model: 'nomic-embed-text' });
    await waitFor(() => expect(screen.getAllByText('present')).toHaveLength(2));
  });

  it('reports unknown rather than missing when Ollama cannot be asked', async () => {
    mockFetch([
      {
        json: {
          ...READY,
          reachable: false,
          ready: false,
          error: 'RuntimeError: connection refused',
          models: READY.models.map((m) => ({ ...m, present: null })),
        },
      },
    ]);
    render(<RequiredModelsPanel />);

    expect(await screen.findByText(/Ollama did not answer/)).toBeInTheDocument();
    expect(screen.getAllByText('unknown')).toHaveLength(2);
    expect(screen.queryByRole('button', { name: /^Pull / })).not.toBeInTheDocument();
  });
});
