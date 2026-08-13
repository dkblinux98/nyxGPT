/**
 * Tests for Support -> Docs, the packaged documentation index (#3745).
 *
 * This page is the whole documentation surface for someone who installed from
 * PyPI or Homebrew and has no checkout, so the states that matter are: the
 * index renders, an unreachable or erroring backend says so instead of
 * showing an empty page, and the "File an Issue" banner -- the only part that
 * needs the network -- never takes the docs down with it when it fails.
 */
import { describe, it, expect } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import SupportDocsIndexPage from '../../../src/app/support/docs/page';

const DOCUMENTS = [
  { slug: 'README', title: 'Documentation', summary: 'Start here.' },
  { slug: 'configuration', title: 'Configuration', summary: 'Every setting nyxGPT reads.' },
  // No summary: the description line must be omitted rather than rendered empty.
  { slug: 'rag', title: 'RAG', summary: '' },
];

const CONTEXT = {
  environment: { version: '3.0.0', platform: 'Linux 6.1 (x86_64)', python: '3.12.1' },
  issue_form_url: 'https://github.com/dkblinux98/nyxGPT/issues/new?template=support.yml',
  network_note: 'Filing an issue opens GitHub in your browser and needs internet access.',
};

function serveDocs(payload: unknown, status = 200) {
  server.use(
    http.get('/api/v1/support/docs', () => HttpResponse.json(payload as object, { status }))
  );
}

function serveContext(payload: unknown, status = 200) {
  server.use(
    http.get('/api/v1/support/context', () => HttpResponse.json(payload as object, { status }))
  );
}

describe('Support docs index page', () => {
  it('lists every packaged document, linking each to its in-app viewer route', async () => {
    serveDocs({ documents: DOCUMENTS });
    serveContext(CONTEXT);
    render(<SupportDocsIndexPage />);

    const readme = await screen.findByRole('link', { name: 'Documentation' });
    expect(readme).toHaveAttribute('href', '/support/docs/README');
    expect(screen.getByRole('link', { name: 'Configuration' })).toHaveAttribute(
      'href',
      '/support/docs/configuration'
    );
    expect(screen.getByText('Every setting nyxGPT reads.')).toBeInTheDocument();
    // A document with no summary renders its title and nothing else.
    expect(screen.getByRole('link', { name: 'RAG' })).toHaveAttribute('href', '/support/docs/rag');
    expect(screen.getByRole('link', { name: /back to chat/i })).toHaveAttribute('href', '/');
  });

  it('shows the running version and the issue link once the context resolves', async () => {
    serveDocs({ documents: DOCUMENTS });
    serveContext(CONTEXT);
    render(<SupportDocsIndexPage />);

    expect(await screen.findByText('3.0.0')).toBeInTheDocument();
    const issueLink = screen.getByRole('link', { name: /file an issue/i });
    expect(issueLink).toHaveAttribute('href', CONTEXT.issue_form_url);
    expect(issueLink).toHaveAttribute('target', '_blank');
    expect(issueLink).toHaveAttribute('rel', 'noopener noreferrer');
    // Docs work offline and filing does not; the page has to say which.
    expect(screen.getByText(CONTEXT.network_note)).toBeInTheDocument();
  });

  it('still renders the docs when the issue link cannot be built', async () => {
    // The exact offline case this page exists for: reading must not depend on
    // the one thing that needs the network.
    serveDocs({ documents: DOCUMENTS });
    server.use(http.get('/api/v1/support/context', () => HttpResponse.error()));
    render(<SupportDocsIndexPage />);

    expect(await screen.findByRole('link', { name: 'Configuration' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /file an issue/i })).not.toBeInTheDocument();
  });

  it('hides the issue banner when the context endpoint returns an error status', async () => {
    serveDocs({ documents: DOCUMENTS });
    serveContext({ detail: 'unavailable' }, 503);
    render(<SupportDocsIndexPage />);

    expect(await screen.findByRole('link', { name: 'Configuration' })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole('link', { name: /file an issue/i })).not.toBeInTheDocument();
    });
  });

  it('reports an HTTP failure from the index endpoint', async () => {
    serveDocs({ detail: 'docs unreadable' }, 500);
    serveContext(CONTEXT);
    render(<SupportDocsIndexPage />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/HTTP 500/);
  });

  it('reports an unreachable API rather than an empty list', async () => {
    server.use(http.get('/api/v1/support/docs', () => HttpResponse.error()));
    serveContext(CONTEXT);
    render(<SupportDocsIndexPage />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/could not reach the nyxGPT API/i);
    expect(screen.queryByText(/loading documentation/i)).not.toBeInTheDocument();
  });

  it('says so plainly when the install shipped no documentation', async () => {
    serveDocs({});
    serveContext(CONTEXT);
    render(<SupportDocsIndexPage />);

    expect(await screen.findByText(/no documentation shipped with this install/i)).toBeInTheDocument();
  });

  it('shows a loading state before the index arrives', () => {
    serveDocs({ documents: DOCUMENTS });
    serveContext(CONTEXT);
    render(<SupportDocsIndexPage />);

    expect(screen.getByText(/loading documentation/i)).toBeInTheDocument();
  });
});
