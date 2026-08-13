/**
 * Tests for Support -> Docs -> one document (#3745).
 *
 * The HTML is rendered and sanitized by the backend and injected here, so the
 * states worth pinning are the three a reader can hit: the document renders,
 * a slug that ships with no document says exactly that (not "backend down"),
 * and an unreachable API is reported rather than leaving a blank page.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import SupportDocumentPage from '../../../src/app/support/docs/[slug]/page';

/**
 * The page takes React 19 async route params and reads them with `use()`.
 * Next hands it an already-settled thenable, so these tests do the same
 * (`status`/`value` are the fields `use()` reads to resolve without
 * suspending) -- suspending on a params promise is a Next runtime concern,
 * not behaviour of this page.
 */
function settledParams(slug: string): Promise<{ slug: string }> {
  const params = Promise.resolve({ slug }) as Promise<{ slug: string }> & {
    status?: string;
    value?: { slug: string };
  };
  params.status = 'fulfilled';
  params.value = { slug };
  return params;
}

function renderDocument(slug: string) {
  return render(<SupportDocumentPage params={settledParams(slug)} />);
}

function serveDocument(slug: string, payload: unknown, status = 200) {
  server.use(
    http.get(`/api/v1/support/docs/${slug}`, () =>
      HttpResponse.json(payload as object, { status })
    )
  );
}

describe('Support document page', () => {
  it('renders the document HTML the backend returned', async () => {
    serveDocument('configuration', {
      slug: 'configuration',
      title: 'Configuration',
      html: '<h1>Configuration</h1><p>Every setting nyxGPT reads.</p>',
    });
    renderDocument('configuration');

    expect(
      await screen.findByRole('heading', { name: 'Configuration', level: 1 })
    ).toBeInTheDocument();
    expect(screen.getByText('Every setting nyxGPT reads.')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /all documentation/i })).toHaveAttribute(
      'href',
      '/support/docs'
    );
  });

  it('shows a loading state while the document is being fetched', async () => {
    serveDocument('rag', { slug: 'rag', title: 'RAG', html: '<h1>RAG</h1>' });
    renderDocument('rag');

    // The loading copy is what stands in for the document until the fetch
    // resolves, so it has to be there on the first paint.
    expect(screen.getByText('Loading…')).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'RAG' })).toBeInTheDocument();
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument();
  });

  it('distinguishes an unknown document from a broken backend', async () => {
    serveDocument('nope', { detail: 'Unknown document' }, 404);
    renderDocument('nope');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/No document named "nope" ships with this version/i);
  });

  it('reports a non-404 HTTP failure with its status', async () => {
    serveDocument('configuration', { detail: 'render failed' }, 500);
    renderDocument('configuration');

    expect(await screen.findByRole('alert')).toHaveTextContent(/HTTP 500/);
  });

  it('reports an unreachable API rather than rendering nothing', async () => {
    server.use(http.get('/api/v1/support/docs/rag', () => HttpResponse.error()));
    renderDocument('rag');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /could not reach the nyxGPT API/i
    );
  });

  it('url-encodes the slug it asks the proxy route for', async () => {
    // The backend is the only judge of what a slug may name; the page must
    // not be the thing that splices one into a path.
    let requested: string | null = null;
    server.use(
      http.get('/api/v1/support/docs/:slug', ({ request }) => {
        requested = new URL(request.url).pathname;
        return HttpResponse.json({ detail: 'Unknown document' }, { status: 404 });
      })
    );
    renderDocument('../secrets');

    await screen.findByRole('alert');
    expect(requested).toBe('/api/v1/support/docs/..%2Fsecrets');
  });
});
