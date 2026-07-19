import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import '@testing-library/jest-dom';
import ModelsPage from '../../../src/app/models/page';

const toastSuccess = vi.fn();
const toastError = vi.fn();

vi.mock('../../../src/contexts/ToastContext', () => ({
  useToast: () => ({
    success: (...a: unknown[]) => toastSuccess(...a),
    error: (...a: unknown[]) => toastError(...a),
  }),
}));

type FetchMock = ReturnType<typeof vi.fn>;

function okJson(body: unknown) {
  return { ok: true, json: async () => body };
}

function failJson(status: number, body: unknown = {}) {
  return { ok: false, status, json: async () => body };
}

describe('ModelsPage CRUD flows', () => {
  let fetchMock: FetchMock;

  beforeEach(() => {
    toastSuccess.mockClear();
    toastError.mockClear();
    fetchMock = vi.fn();
    global.fetch = fetchMock as unknown as typeof fetch;
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders the model list after a successful load', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ models: ['llama3.1:8b', 'qwen2.5:7b'] }));
    render(<ModelsPage />);
    expect(await screen.findByText('llama3.1:8b')).toBeInTheDocument();
    expect(screen.getByText('qwen2.5:7b')).toBeInTheDocument();
  });

  it('treats a missing models field as an empty list and shows the empty state', async () => {
    fetchMock.mockResolvedValueOnce(okJson({}));
    render(<ModelsPage />);
    expect(await screen.findByText(/No models found/)).toBeInTheDocument();
  });

  it('shows an error with retry when the initial load fails', async () => {
    fetchMock.mockResolvedValueOnce(failJson(500));
    render(<ModelsPage />);
    expect(await screen.findByText(/Failed to load models/)).toBeInTheDocument();
    expect(screen.getByText(/HTTP 500/)).toBeInTheDocument();

    // Retry succeeds
    fetchMock.mockResolvedValueOnce(okJson({ models: ['m1'] }));
    fireEvent.click(screen.getByRole('button', { name: /Try Again|Retry/i }));
    expect(await screen.findByText('m1')).toBeInTheDocument();
  });

  it('shows the error message for a non-Error rejection', async () => {
    fetchMock.mockRejectedValueOnce('plain string failure');
    render(<ModelsPage />);
    expect(await screen.findByText(/plain string failure/)).toBeInTheDocument();
  });

  it('reloads the list via the Refresh button', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ models: ['first'] }));
    render(<ModelsPage />);
    await screen.findByText('first');

    fetchMock.mockResolvedValueOnce(okJson({ models: ['second'] }));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByText('second')).toBeInTheDocument();
    expect(screen.queryByText('first')).not.toBeInTheDocument();
  });

  it('pulls a model successfully, clears the input, reloads, and toasts', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ models: [] })); // initial load
    render(<ModelsPage />);
    await screen.findByText(/No models found/);

    const input = screen.getByPlaceholderText(/Model name/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'newmodel:3b' } });

    fetchMock.mockResolvedValueOnce(okJson({})); // POST pull
    fetchMock.mockResolvedValueOnce(okJson({ models: ['newmodel:3b'] })); // reload

    fireEvent.click(screen.getByRole('button', { name: 'Pull' }));

    expect(await screen.findByText('newmodel:3b')).toBeInTheDocument();
    expect(input.value).toBe('');
    expect(toastSuccess).toHaveBeenCalledWith('Model pulled successfully');
    const postCall = fetchMock.mock.calls[1];
    expect(postCall[0]).toBe('/api/models');
    expect(postCall[1].method).toBe('POST');
    expect(JSON.parse(postCall[1].body)).toEqual({ model: 'newmodel:3b' });
  });

  it('ignores submit with a blank model name', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ models: [] }));
    render(<ModelsPage />);
    await screen.findByText(/No models found/);

    const form = screen.getByPlaceholderText(/Model name/i).closest('form')!;
    fireEvent.submit(form);
    // only the initial load call
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('surfaces a pull failure with the API detail message and error toast', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ models: [] }));
    render(<ModelsPage />);
    await screen.findByText(/No models found/);

    fireEvent.change(screen.getByPlaceholderText(/Model name/i), {
      target: { value: 'badmodel:1b' },
    });
    fetchMock.mockResolvedValueOnce(failJson(422, { detail: 'model not found upstream' }));
    fireEvent.click(screen.getByRole('button', { name: 'Pull' }));

    expect(await screen.findByText(/Failed to pull model/)).toBeInTheDocument();
    expect(screen.getByText(/model not found upstream/)).toBeInTheDocument();
    expect(toastError).toHaveBeenCalledWith('Failed to pull model: model not found upstream');
  });

  it('falls back to the HTTP status when a pull failure has no detail', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ models: [] }));
    render(<ModelsPage />);
    await screen.findByText(/No models found/);

    fireEvent.change(screen.getByPlaceholderText(/Model name/i), {
      target: { value: 'x:2b' },
    });
    fetchMock.mockResolvedValueOnce(failJson(503, {}));
    fireEvent.click(screen.getByRole('button', { name: 'Pull' }));
    expect(await screen.findByText(/HTTP 503/)).toBeInTheDocument();
  });

  it('retries a failed pull from the inline ErrorMessage', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ models: [] }));
    render(<ModelsPage />);
    await screen.findByText(/No models found/);

    fireEvent.change(screen.getByPlaceholderText(/Model name/i), {
      target: { value: 'retry:2b' },
    });
    fetchMock.mockResolvedValueOnce(failJson(500, { detail: 'first failure' }));
    fireEvent.click(screen.getByRole('button', { name: 'Pull' }));
    await screen.findByText(/first failure/);

    fetchMock.mockResolvedValueOnce(okJson({})); // retried POST
    fetchMock.mockResolvedValueOnce(okJson({ models: ['retry:2b'] })); // reload
    const retryButtons = screen.getAllByRole('button', { name: /Try Again|Retry/i });
    fireEvent.click(retryButtons[0]);
    expect(await screen.findByText('retry:2b')).toBeInTheDocument();
  });

  it('deletes a model after confirm, reloads, and toasts success', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ models: ['doomed:1b'] }));
    render(<ModelsPage />);
    await screen.findByText('doomed:1b');

    fetchMock.mockResolvedValueOnce(okJson({})); // DELETE
    fetchMock.mockResolvedValueOnce(okJson({ models: [] })); // reload
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

    expect(await screen.findByText(/No models found/)).toBeInTheDocument();
    expect(toastSuccess).toHaveBeenCalledWith('Model deleted successfully');
    const delCall = fetchMock.mock.calls[1];
    expect(String(delCall[0])).toContain('model=doomed%3A1b');
    expect(delCall[1].method).toBe('DELETE');
  });

  it('does nothing when the delete confirm is cancelled', async () => {
    (globalThis.confirm as ReturnType<typeof vi.fn>).mockReturnValueOnce(false);
    fetchMock.mockResolvedValueOnce(okJson({ models: ['kept:1b'] }));
    render(<ModelsPage />);
    await screen.findByText('kept:1b');

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(fetchMock).toHaveBeenCalledTimes(1); // load only
    expect(screen.getByText('kept:1b')).toBeInTheDocument();
  });

  it('surfaces a delete failure via error toast (detail message)', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ models: ['stuck:1b'] }));
    render(<ModelsPage />);
    await screen.findByText('stuck:1b');

    fetchMock.mockResolvedValueOnce(failJson(409, { detail: 'model in use' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('Failed to delete model: model in use')
    );
    // model remains listed
    expect(screen.getByText('stuck:1b')).toBeInTheDocument();
  });

  it('reports non-Error rejections via String(e) for pull and delete', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ models: ['m:1b'] }));
    render(<ModelsPage />);
    await screen.findByText('m:1b');

    fireEvent.change(screen.getByPlaceholderText(/Model name/i), {
      target: { value: 'x:2b' },
    });
    fetchMock.mockRejectedValueOnce('raw pull failure');
    fireEvent.click(screen.getByRole('button', { name: 'Pull' }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('Failed to pull model: raw pull failure')
    );

    fetchMock.mockRejectedValueOnce('raw delete failure');
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('Failed to delete model: raw delete failure')
    );
  });

  it('falls back to HTTP status for a delete failure without detail', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ models: ['m:1b'] }));
    render(<ModelsPage />);
    await screen.findByText('m:1b');

    fetchMock.mockResolvedValueOnce(failJson(500, {}));
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('Failed to delete model: HTTP 500')
    );
  });
});
