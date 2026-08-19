/**
 * Tests for the Docs button / Attached Documents panel visibility in ChatPane.
 *
 * Issue #3130 acceptance failure: the Docs toggle button and the Attached Documents
 * panel were gated on `ragEnabled`, so users who disabled RAG after attaching documents
 * could no longer see or remove their attachments.
 *
 * Fixed behaviour:
 *  - Docs button: visible when `ragEnabled === true` OR when `attachedDocIds.length > 0`
 *  - Attached Docs panel: visible when `showAttachedDocs === true` (independent of ragEnabled)
 *
 * These tests render the actual ChatPane component so that any regression in
 * ChatPane.tsx would cause these tests to fail (unlike pure-logic unit tests
 * that only exercise local helper functions).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import React from 'react';
import ChatPane from '../../../../src/app/components/ChatPane';
import type { VirtuosoMockProps } from '../../../mocks/virtuoso';

// ---------------------------------------------------------------------------
// Module mocks
// ---------------------------------------------------------------------------

// Prevent react-virtuoso from throwing in happy-dom (no real layout engine)
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: VirtuosoMockProps) => {
    const items = (data ?? []).slice(0, 10);
    return React.createElement(
      'div',
      { 'data-testid': 'virtualized-chat' },
      ...items.map((item: unknown, index: number) =>
        React.createElement('div', { key: index }, itemContent(index, item))
      )
    );
  },
  VirtuosoHandle: vi.fn(),
}));

vi.mock('../../../../src/contexts/ToastContext', () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }),
}));

// ---------------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------------

function buildMockFetch({
  ragEnabled = false,
  attachedDocIds = [] as string[],
} = {}) {
  return vi.fn().mockImplementation((url: string) => {
    if (url.includes('/api/models') || url.includes('/models')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ models: ['llama3.1:8b'] }),
      });
    }
    if (url.includes('/metadata')) {
      return Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve({
            rag_enabled: ragEnabled,
            title: 'Test Session',
            model: 'llama3.1:8b',
          }),
      });
    }
    if (url.includes('/documents')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ attached_doc_ids: attachedDocIds }),
      });
    }
    if (url.includes('/api/v1/sessions/')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ messages: [], total: 0 }),
      });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
  });
}

// ---------------------------------------------------------------------------
// Docs button visibility
// ---------------------------------------------------------------------------

describe('ChatPane — Docs button visibility', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('hides the Docs button when RAG is disabled and no docs are attached', async () => {
    global.fetch = buildMockFetch({ ragEnabled: false, attachedDocIds: [] });

    render(React.createElement(ChatPane, { sessionName: 'test-session' }));

    // Allow all async effects / fetches to settle, then assert absence
    await waitFor(() => {
      expect(screen.queryByTitle('Manage force-included documents')).toBeNull();
    });
  });

  it('shows the Docs button when RAG is disabled but docs are attached (regression fix)', async () => {
    // Before the fix this button was hidden because ragEnabled was false,
    // making it impossible for the user to detach documents.
    global.fetch = buildMockFetch({ ragEnabled: false, attachedDocIds: ['doc-a', 'doc-b'] });

    render(React.createElement(ChatPane, { sessionName: 'test-session' }));

    await waitFor(() => {
      expect(screen.getByTitle('Manage force-included documents')).toBeInTheDocument();
    });
  });

  it('shows the Docs button when RAG is enabled and no docs are attached', async () => {
    global.fetch = buildMockFetch({ ragEnabled: true, attachedDocIds: [] });

    render(React.createElement(ChatPane, { sessionName: 'test-session' }));

    await waitFor(() => {
      expect(screen.getByTitle('Manage force-included documents')).toBeInTheDocument();
    });
  });

  it('shows the Docs button when RAG is enabled and docs are attached', async () => {
    global.fetch = buildMockFetch({ ragEnabled: true, attachedDocIds: ['doc-a'] });

    render(React.createElement(ChatPane, { sessionName: 'test-session' }));

    await waitFor(() => {
      expect(screen.getByTitle('Manage force-included documents')).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Attached Documents panel visibility
// ---------------------------------------------------------------------------

describe('ChatPane — Attached Documents panel visibility', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows the panel after clicking Docs button, regardless of ragEnabled state', async () => {
    // Regression test: before the fix the panel was gated on ragEnabled, so
    // attached documents became invisible when RAG was turned off.
    global.fetch = buildMockFetch({ ragEnabled: false, attachedDocIds: ['doc-a'] });

    render(React.createElement(ChatPane, { sessionName: 'test-session' }));

    // Wait for the Docs button to appear (docs loaded from API)
    const docsButton = await screen.findByTitle('Manage force-included documents');

    // Panel is hidden initially
    expect(screen.queryByText('Force-Included Documents')).toBeNull();

    // Click the Docs button to open the panel
    fireEvent.click(docsButton);

    await waitFor(() => {
      expect(screen.getByText('Force-Included Documents')).toBeInTheDocument();
    });
  });

  it('hides the panel after toggling Docs button closed', async () => {
    global.fetch = buildMockFetch({ ragEnabled: false, attachedDocIds: ['doc-a'] });

    render(React.createElement(ChatPane, { sessionName: 'test-session' }));

    const docsButton = await screen.findByTitle('Manage force-included documents');

    // Open then close
    fireEvent.click(docsButton);
    await waitFor(() => {
      expect(screen.getByText('Force-Included Documents')).toBeInTheDocument();
    });

    fireEvent.click(docsButton);
    await waitFor(() => {
      expect(screen.queryByText('Force-Included Documents')).toBeNull();
    });
  });
});
