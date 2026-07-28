import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import ModelsPage from '../../../src/app/models/page';
import { estimateModelResourceHint } from '../../../src/app/models/model-hints';

vi.mock('../../../src/contexts/ToastContext', () => ({
  useToast: () => ({
    success: vi.fn(),
    error: vi.fn(),
  }),
}));

describe('estimateModelResourceHint', () => {
  it('returns null when the model name has no parameter-count suffix', () => {
    expect(estimateModelResourceHint('mystery-model')).toBeNull();
  });

  it('classifies sub-1B models as Small', () => {
    expect(estimateModelResourceHint('qwen2.5:0.5b')).toMatch(/Small model/);
  });

  it('classifies the llama3.1:8b boundary case as Large with a memory warning', () => {
    // Regression guard: this is the exact model named in issue #3192 and in
    // docs/performance.md's table (8-16 GB) -- it must not fall into the
    // "Medium" bucket with no warning.
    const hint = estimateModelResourceHint('llama3.1:8b');
    expect(hint).toMatch(/Large model/);
    expect(hint).toMatch(/enough free memory/);
  });

  it('classifies models just under the boundary as Medium', () => {
    expect(estimateModelResourceHint('qwen2.5:7b')).toMatch(/Medium model/);
  });

  it('classifies large multi-digit parameter counts as Large', () => {
    const hint = estimateModelResourceHint('llama3.1:70b');
    expect(hint).toMatch(/Large model/);
    expect(hint).toMatch(/enough free memory/);
  });
});

describe('ModelsPage resource hint UI', () => {
  beforeEach(() => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ models: [] }),
    }) as unknown as typeof fetch;
  });

  it('shows a large-model warning when the user types llama3.1:8b', async () => {
    render(<ModelsPage />);

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());

    const input = screen.getByPlaceholderText(/Model name/i);
    fireEvent.change(input, { target: { value: 'llama3.1:8b' } });

    expect(await screen.findByText(/Large model/)).toBeInTheDocument();
    expect(screen.getByText(/enough free memory/)).toBeInTheDocument();
  });
});
