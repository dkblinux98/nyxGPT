import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import CollectionsPage from '../../../src/app/admin/collections/page';

const sampleCollections = [
  {
    name: 'default',
    doc_count: 12,
    chunk_count: 340,
    embedding_models: [],
  },
  {
    name: 'notes',
    doc_count: 3,
    chunk_count: 20,
    embedding_models: ['nomic-embed-text', 'mxbai-embed-large'],
  },
];

const sampleSettings = {
  settings: {
    embedding_model: 'nomic-embed-text',
    chunk_size: 800,
    chunk_overlap: 100,
  },
};

function mockCollections(collections = sampleCollections) {
  server.use(http.get('/api/v1/rag/collections', () => HttpResponse.json({ collections })));
}

function createModal() {
  return screen.getByRole('heading', { name: 'Create Collection' }).closest('div') as HTMLElement;
}

function settingsModal() {
  return screen.getByRole('heading', { name: /Collection Settings:/ }).closest('div') as HTMLElement;
}

describe('CollectionsPage', () => {
  beforeEach(() => {
    global.alert = vi.fn();
  });

  it('renders the back to chat link and keeps the Create Collection action', async () => {
    mockCollections([]);

    render(<CollectionsPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'RAG Collections' })).toBeInTheDocument();
    });

    const link = screen.getByRole('link', { name: /back to chat/i });
    expect(link).toHaveAttribute('href', '/');
    expect(screen.getByRole('button', { name: /create collection/i })).toBeInTheDocument();
  });

  it('shows a valid placeholder example that matches the naming rule it documents', async () => {
    mockCollections([]);
    const user = userEvent.setup();

    render(<CollectionsPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /create collection/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /create collection/i }));

    // The placeholder must itself be a name the field's own validation accepts --
    // regression coverage for the bug where the example (`my-collection`)
    // contained a hyphen, which the backend rejects.
    const nameInput = screen.getByLabelText(/collection name/i) as HTMLInputElement;
    expect(nameInput.placeholder).toBe('my_collection');
    expect(nameInput.placeholder).toMatch(/^[a-zA-Z0-9_]+$/);
    expect(screen.getByText(/letters, numbers, and underscores only/i)).toBeInTheDocument();
  });

  it('shows the empty state when there are no collections', async () => {
    mockCollections([]);

    render(<CollectionsPage />);

    await waitFor(() => {
      expect(screen.getByText('No collections found')).toBeInTheDocument();
    });
  });

  it('shows the empty state when the response omits the collections field', async () => {
    server.use(http.get('/api/v1/rag/collections', () => HttpResponse.json({})));

    render(<CollectionsPage />);

    await waitFor(() => {
      expect(screen.getByText('No collections found')).toBeInTheDocument();
    });
  });

  it('shows an error message and allows retrying when loading fails', async () => {
    server.use(http.get('/api/v1/rag/collections', () => new HttpResponse(null, { status: 500 })));

    render(<CollectionsPage />);

    await waitFor(() => {
      expect(screen.getByText(/Failed to load collections: HTTP 500/)).toBeInTheDocument();
    });

    mockCollections([]);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /retry/i }));

    await waitFor(() => {
      expect(screen.getByText('No collections found')).toBeInTheDocument();
    });
  });

  it('renders collection cards including the default badge, models, and no-models case', async () => {
    mockCollections();

    render(<CollectionsPage />);

    await waitFor(() => {
      expect(screen.getByText('(Default)')).toBeInTheDocument();
    });
    expect(screen.getByText('12')).toBeInTheDocument();
    expect(screen.getByText('340')).toBeInTheDocument();
    expect(screen.getByText('None')).toBeInTheDocument();
    expect(screen.getByText('nomic-embed-text')).toBeInTheDocument();
    expect(screen.getByText('mxbai-embed-large')).toBeInTheDocument();

    // Default collection has no "Clear Collection" button
    const defaultCard = screen.getByText('default').closest('div')!.parentElement!.parentElement!;
    expect(within(defaultCard).queryByRole('button', { name: /clear collection/i })).not.toBeInTheDocument();
  });

  it('shows delete confirmation, allows cancelling, then walks every delete error branch before succeeding', async () => {
    mockCollections();
    const user = userEvent.setup();

    render(<CollectionsPage />);

    await waitFor(() => {
      expect(screen.getByText('notes')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /clear collection/i }));
    expect(screen.getByText(/Are you sure you want to clear this collection/)).toBeInTheDocument();
    expect(screen.getByText(/permanently delete all 3 documents and 20 chunks/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /cancel/i }));
    expect(screen.queryByText(/Are you sure you want to clear this collection/)).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /clear collection/i }));

    // errorData.error.message branch (the real API error envelope shape --
    // see http_exception_handler in src/nyxgpt/app.py)
    server.use(
      http.delete('/api/v1/rag/collections/notes', () =>
        HttpResponse.json(
          { error: { code: 'http_error', message: 'Collection is in use', details: null, request_id: 'req-1' } },
          { status: 400 }
        )
      )
    );
    await user.click(screen.getByRole('button', { name: /yes, clear collection/i }));
    await waitFor(() => {
      expect(screen.getByText(/Failed to delete collection: Collection is in use/)).toBeInTheDocument();
    });

    // errorData.error string branch (the Next.js proxy route's own catch-block
    // shape, e.g. on a network failure between the proxy and the backend)
    server.use(
      http.delete('/api/v1/rag/collections/notes', () =>
        HttpResponse.json({ error: 'Backend returned 502' }, { status: 502 })
      )
    );
    await user.click(screen.getByRole('button', { name: /yes, clear collection/i }));
    await waitFor(() => {
      expect(screen.getByText(/Failed to delete collection: Backend returned 502/)).toBeInTheDocument();
    });

    // errorData.detail branch (error absent)
    server.use(
      http.delete('/api/v1/rag/collections/notes', () =>
        HttpResponse.json({ detail: 'referenced by an active session' }, { status: 400 })
      )
    );
    await user.click(screen.getByRole('button', { name: /yes, clear collection/i }));
    await waitFor(() => {
      expect(screen.getByText(/Failed to delete collection: referenced by an active session/)).toBeInTheDocument();
    });

    // HTTP status fallback branch (neither error nor detail present)
    server.use(http.delete('/api/v1/rag/collections/notes', () => HttpResponse.json({}, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /yes, clear collection/i }));
    await waitFor(() => {
      expect(screen.getByText(/Failed to delete collection: HTTP 500/)).toBeInTheDocument();
    });

    // Unparseable body branch (json() rejects, falls back to "Unknown error")
    server.use(http.delete('/api/v1/rag/collections/notes', () => new HttpResponse(null, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /yes, clear collection/i }));
    await waitFor(() => {
      expect(screen.getByText(/Failed to delete collection: Unknown error/)).toBeInTheDocument();
    });

    // Finally succeed
    server.use(
      http.delete('/api/v1/rag/collections/notes', () => HttpResponse.json({ success: true })),
      http.get('/api/v1/rag/collections', () => HttpResponse.json({ collections: [sampleCollections[0]] }))
    );
    await user.click(screen.getByRole('button', { name: /yes, clear collection/i }));
    await waitFor(() => {
      expect(screen.queryByText('notes')).not.toBeInTheDocument();
    });
  });

  it('validates the create collection form, closes on idle overlay click, and is a no-op overlay click while creating', async () => {
    mockCollections([]);
    const user = userEvent.setup();

    render(<CollectionsPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /create collection/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /create collection/i }));
    expect(screen.getByRole('heading', { name: 'Create Collection' })).toBeInTheDocument();

    // Empty name
    await user.click(within(createModal()).getByRole('button', { name: 'Create Collection' }));
    expect(global.alert).toHaveBeenCalledWith('Collection name is required');

    // Hyphenated name (the placeholder used to suggest this was valid -- it isn't)
    await user.type(screen.getByLabelText(/collection name/i), 'test-collection');
    await user.click(within(createModal()).getByRole('button', { name: 'Create Collection' }));
    expect(global.alert).toHaveBeenCalledWith(
      'Collection names may contain only letters, digits, and underscores (no hyphens, spaces, or other characters).'
    );

    // Over-length name
    await user.clear(screen.getByLabelText(/collection name/i));
    await user.type(screen.getByLabelText(/collection name/i), 'a'.repeat(38));
    await user.click(within(createModal()).getByRole('button', { name: 'Create Collection' }));
    expect(global.alert).toHaveBeenCalledWith('Collection name must be at most 37 characters.');

    // Invalid dimension (explicit 0)
    await user.clear(screen.getByLabelText(/collection name/i));
    await user.type(screen.getByLabelText(/collection name/i), 'my_collection');
    await user.clear(screen.getByLabelText(/embedding dimension/i));
    await user.type(screen.getByLabelText(/embedding dimension/i), '0');
    await user.click(within(createModal()).getByRole('button', { name: 'Create Collection' }));
    expect(global.alert).toHaveBeenCalledWith('Valid embedding dimension is required');

    // Invalid dimension (empty -> NaN)
    await user.clear(screen.getByLabelText(/embedding dimension/i));
    await user.click(within(createModal()).getByRole('button', { name: 'Create Collection' }));
    expect(global.alert).toHaveBeenCalledWith('Valid embedding dimension is required');

    // Idle overlay click closes the modal
    const overlay = createModal().parentElement!;
    await user.click(overlay);
    expect(screen.queryByRole('heading', { name: 'Create Collection' })).not.toBeInTheDocument();

    // The Cancel button also closes the modal
    await user.click(screen.getByRole('button', { name: /create collection/i }));
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('heading', { name: 'Create Collection' })).not.toBeInTheDocument();

    // Reopen, fill in valid data, and hold the create request open to test the busy overlay no-op
    await user.click(screen.getByRole('button', { name: /create collection/i }));
    await user.type(screen.getByLabelText(/collection name/i), 'my_collection');
    await user.clear(screen.getByLabelText(/embedding dimension/i));
    await user.type(screen.getByLabelText(/embedding dimension/i), '768');

    let resolveCreate: () => void = () => {};
    server.use(
      http.post('/api/v1/rag/collections', async () => {
        await new Promise<void>((resolve) => {
          resolveCreate = resolve;
        });
        return HttpResponse.json({ success: true });
      })
    );

    await user.click(within(createModal()).getByRole('button', { name: 'Create Collection' }));

    await waitFor(() => {
      expect(within(createModal()).getByRole('button', { name: /creating/i })).toBeDisabled();
    });

    const busyOverlay = createModal().parentElement!;
    await user.click(busyOverlay);
    expect(screen.getByRole('heading', { name: 'Create Collection' })).toBeInTheDocument();

    mockCollections([]);
    resolveCreate();

    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: 'Create Collection' })).not.toBeInTheDocument();
    });
  });

  it('walks every create-collection error branch before succeeding', async () => {
    mockCollections([]);
    const user = userEvent.setup();

    render(<CollectionsPage />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /create collection/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /create collection/i }));
    await user.type(screen.getByLabelText(/collection name/i), 'my_collection');

    // errorData.error.message branch (the real API error envelope shape --
    // e.g. a 409 duplicate-collection-name rejection)
    server.use(
      http.post('/api/v1/rag/collections', () =>
        HttpResponse.json(
          { error: { code: 'http_error', message: 'name taken', details: null, request_id: 'req-2' } },
          { status: 409 }
        )
      )
    );
    await user.click(within(createModal()).getByRole('button', { name: 'Create Collection' }));
    await waitFor(() => {
      expect(screen.getByText(/Failed to create collection: name taken/)).toBeInTheDocument();
    });

    // errorData.detail branch (error absent)
    server.use(
      http.post('/api/v1/rag/collections', () =>
        HttpResponse.json({ detail: 'name already exists' }, { status: 409 })
      )
    );
    await user.click(within(createModal()).getByRole('button', { name: 'Create Collection' }));
    await waitFor(() => {
      expect(screen.getByText(/Failed to create collection: name already exists/)).toBeInTheDocument();
    });

    // HTTP status fallback branch
    server.use(http.post('/api/v1/rag/collections', () => HttpResponse.json({}, { status: 500 })));
    await user.click(within(createModal()).getByRole('button', { name: 'Create Collection' }));
    await waitFor(() => {
      expect(screen.getByText(/Failed to create collection: HTTP 500/)).toBeInTheDocument();
    });

    // extractErrorMessage guard branches: a JSON body that parses but is not
    // an object (null / bare string) must fall through to the HTTP fallback
    // rather than being unwrapped.
    server.use(http.post('/api/v1/rag/collections', () => HttpResponse.json(null, { status: 502 })));
    await user.click(within(createModal()).getByRole('button', { name: 'Create Collection' }));
    await waitFor(() => {
      expect(screen.getByText(/Failed to create collection: HTTP 502/)).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/rag/collections', () => HttpResponse.json('bare string', { status: 503 })));
    await user.click(within(createModal()).getByRole('button', { name: 'Create Collection' }));
    await waitFor(() => {
      expect(screen.getByText(/Failed to create collection: HTTP 503/)).toBeInTheDocument();
    });

    // Unparseable body branch
    server.use(http.post('/api/v1/rag/collections', () => new HttpResponse(null, { status: 500 })));
    await user.click(within(createModal()).getByRole('button', { name: 'Create Collection' }));
    await waitFor(() => {
      expect(screen.getByText(/Failed to create collection: Unknown error/)).toBeInTheDocument();
    });

    // Finally succeed, including an optional embedding model
    await user.type(screen.getByLabelText(/embedding model/i), 'nomic-embed-text');
    server.use(
      http.post('/api/v1/rag/collections', () => HttpResponse.json({ success: true })),
      http.get('/api/v1/rag/collections', () => HttpResponse.json({ collections: [sampleCollections[0]] }))
    );
    await user.click(within(createModal()).getByRole('button', { name: 'Create Collection' }));
    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: 'Create Collection' })).not.toBeInTheDocument();
    });
  });

  it('views collection settings, edits, saves, and closes via the Close button', async () => {
    mockCollections();
    const user = userEvent.setup();

    render(<CollectionsPage />);

    await waitFor(() => {
      expect(screen.getByText('notes')).toBeInTheDocument();
    });

    server.use(http.get('/api/v1/rag/collections/notes/settings', () => HttpResponse.json(sampleSettings)));

    const viewSettingsButtons = screen.getAllByRole('button', { name: /view settings/i });
    await user.click(viewSettingsButtons[1]);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Collection Settings: notes' })).toBeInTheDocument();
    });
    expect(screen.getByDisplayValue('nomic-embed-text')).toBeInTheDocument();
    expect(screen.getByDisplayValue('800')).toBeInTheDocument();
    expect(screen.getByDisplayValue('100')).toBeInTheDocument();

    server.use(
      http.put('/api/v1/rag/collections/notes/settings', () =>
        HttpResponse.json({
          settings: { embedding_model: 'nomic-embed-text', chunk_size: 1000, chunk_overlap: 100 },
        })
      )
    );

    const chunkSizeInput = screen.getByDisplayValue('800');
    await user.clear(chunkSizeInput);
    await user.type(chunkSizeInput, '1000');

    const embeddingModelInput = screen.getByDisplayValue('nomic-embed-text');
    await user.clear(embeddingModelInput);
    await user.type(embeddingModelInput, 'mxbai-embed-large');

    const chunkOverlapInput = screen.getByDisplayValue('100');
    await user.clear(chunkOverlapInput);
    await user.type(chunkOverlapInput, '120');

    await user.click(screen.getByRole('button', { name: /save settings/i }));

    await waitFor(() => {
      expect(global.alert).toHaveBeenCalledWith('Settings saved successfully');
    });

    await user.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('heading', { name: 'Collection Settings: notes' })).not.toBeInTheDocument();
  });

  it('handles blank optional settings and closes via the overlay click', async () => {
    mockCollections();
    const user = userEvent.setup();

    render(<CollectionsPage />);

    await waitFor(() => {
      expect(screen.getByText('notes')).toBeInTheDocument();
    });

    server.use(
      http.get('/api/v1/rag/collections/notes/settings', () =>
        HttpResponse.json({ settings: { embedding_model: null, chunk_size: null, chunk_overlap: null } })
      )
    );

    const viewSettingsButtons = screen.getAllByRole('button', { name: /view settings/i });
    await user.click(viewSettingsButtons[1]);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Collection Settings: notes' })).toBeInTheDocument();
    });

    server.use(
      http.put('/api/v1/rag/collections/notes/settings', () =>
        HttpResponse.json({ settings: { embedding_model: null, chunk_size: null, chunk_overlap: null } })
      )
    );

    await user.click(screen.getByRole('button', { name: /save settings/i }));
    await waitFor(() => {
      expect(global.alert).toHaveBeenCalledWith('Settings saved successfully');
    });

    const overlay = settingsModal().parentElement!;
    await user.click(overlay);

    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: 'Collection Settings: notes' })).not.toBeInTheDocument();
    });
  });

  it('walks every view-settings error branch', async () => {
    mockCollections();
    const user = userEvent.setup();

    render(<CollectionsPage />);

    await waitFor(() => {
      expect(screen.getByText('notes')).toBeInTheDocument();
    });
    const viewSettingsButtons = screen.getAllByRole('button', { name: /view settings/i });

    // errorData.error.message branch (the real API error envelope shape)
    server.use(
      http.get('/api/v1/rag/collections/notes/settings', () =>
        HttpResponse.json(
          { error: { code: 'http_error', message: 'settings unavailable', details: null, request_id: 'req-3' } },
          { status: 500 }
        )
      )
    );
    await user.click(viewSettingsButtons[1]);
    await waitFor(() => {
      expect(screen.getByText(/Failed to load settings: settings unavailable/)).toBeInTheDocument();
    });
    expect(screen.queryByRole('heading', { name: 'Collection Settings: notes' })).not.toBeInTheDocument();

    // errorData.detail branch (error absent)
    server.use(
      http.get('/api/v1/rag/collections/notes/settings', () =>
        HttpResponse.json({ detail: 'store offline' }, { status: 500 })
      )
    );
    await user.click(viewSettingsButtons[1]);
    await waitFor(() => {
      expect(screen.getByText(/Failed to load settings: store offline/)).toBeInTheDocument();
    });

    // HTTP status fallback branch
    server.use(http.get('/api/v1/rag/collections/notes/settings', () => HttpResponse.json({}, { status: 503 })));
    await user.click(viewSettingsButtons[1]);
    await waitFor(() => {
      expect(screen.getByText(/Failed to load settings: HTTP 503/)).toBeInTheDocument();
    });

    // Unparseable body branch
    server.use(
      http.get('/api/v1/rag/collections/notes/settings', () => new HttpResponse(null, { status: 500 }))
    );
    await user.click(viewSettingsButtons[1]);
    await waitFor(() => {
      expect(screen.getByText(/Failed to load settings: Unknown error/)).toBeInTheDocument();
    });
  });

  it('walks every save-settings error branch', async () => {
    mockCollections();
    const user = userEvent.setup();

    render(<CollectionsPage />);

    await waitFor(() => {
      expect(screen.getByText('notes')).toBeInTheDocument();
    });

    server.use(http.get('/api/v1/rag/collections/notes/settings', () => HttpResponse.json(sampleSettings)));

    const viewSettingsButtons = screen.getAllByRole('button', { name: /view settings/i });
    await user.click(viewSettingsButtons[1]);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Collection Settings: notes' })).toBeInTheDocument();
    });

    // errorData.error.message branch (the real API error envelope shape)
    server.use(
      http.put('/api/v1/rag/collections/notes/settings', () =>
        HttpResponse.json(
          { error: { code: 'http_error', message: 'invalid chunk size', details: null, request_id: 'req-4' } },
          { status: 400 }
        )
      )
    );
    await user.click(screen.getByRole('button', { name: /save settings/i }));
    await waitFor(() => {
      expect(screen.getByText(/Failed to save settings: invalid chunk size/)).toBeInTheDocument();
    });

    // errorData.detail branch (error absent)
    server.use(
      http.put('/api/v1/rag/collections/notes/settings', () =>
        HttpResponse.json({ detail: 'overlap exceeds chunk size' }, { status: 400 })
      )
    );
    await user.click(screen.getByRole('button', { name: /save settings/i }));
    await waitFor(() => {
      expect(screen.getByText(/Failed to save settings: overlap exceeds chunk size/)).toBeInTheDocument();
    });

    // HTTP status fallback branch
    server.use(http.put('/api/v1/rag/collections/notes/settings', () => HttpResponse.json({}, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /save settings/i }));
    await waitFor(() => {
      expect(screen.getByText(/Failed to save settings: HTTP 500/)).toBeInTheDocument();
    });

    // Unparseable body branch
    server.use(
      http.put('/api/v1/rag/collections/notes/settings', () => new HttpResponse(null, { status: 500 }))
    );
    await user.click(screen.getByRole('button', { name: /save settings/i }));
    await waitFor(() => {
      expect(screen.getByText(/Failed to save settings: Unknown error/)).toBeInTheDocument();
    });
  });

  it('falls back to String(e) when a request rejects with a non-Error value', async () => {
    mockCollections();
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(global, 'fetch');

    fetchSpy.mockImplementationOnce(() => Promise.reject('network gremlin'));
    render(<CollectionsPage />);
    await waitFor(() => {
      expect(screen.getByText('network gremlin')).toBeInTheDocument();
    });

    mockCollections();
    await user.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => {
      expect(screen.getByText('notes')).toBeInTheDocument();
    });

    // handleDeleteCollection
    await user.click(screen.getByRole('button', { name: /clear collection/i }));
    fetchSpy.mockImplementationOnce(() => Promise.reject('delete gremlin'));
    await user.click(screen.getByRole('button', { name: /yes, clear collection/i }));
    await waitFor(() => {
      expect(screen.getByText('Failed to delete collection: delete gremlin')).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: /cancel/i }));

    // handleCreateCollection
    await user.click(screen.getByRole('button', { name: /create collection/i }));
    await user.type(screen.getByLabelText(/collection name/i), 'my_collection');
    fetchSpy.mockImplementationOnce(() => Promise.reject('create gremlin'));
    await user.click(within(createModal()).getByRole('button', { name: 'Create Collection' }));
    await waitFor(() => {
      expect(screen.getByText('Failed to create collection: create gremlin')).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    // handleViewSettings
    const viewSettingsButtons = screen.getAllByRole('button', { name: /view settings/i });
    fetchSpy.mockImplementationOnce(() => Promise.reject('settings gremlin'));
    await user.click(viewSettingsButtons[1]);
    await waitFor(() => {
      expect(screen.getByText('Failed to load settings: settings gremlin')).toBeInTheDocument();
    });

    // handleSaveSettings
    server.use(http.get('/api/v1/rag/collections/notes/settings', () => HttpResponse.json(sampleSettings)));
    await user.click(viewSettingsButtons[1]);
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Collection Settings: notes' })).toBeInTheDocument();
    });
    fetchSpy.mockImplementationOnce(() => Promise.reject('save gremlin'));
    await user.click(screen.getByRole('button', { name: /save settings/i }));
    await waitFor(() => {
      expect(screen.getByText('Failed to save settings: save gremlin')).toBeInTheDocument();
    });

    fetchSpy.mockRestore();
  });
});
