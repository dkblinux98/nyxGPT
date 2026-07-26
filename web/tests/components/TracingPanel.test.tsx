import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import '@testing-library/jest-dom';
import TracingPanel from '../../src/components/TracingPanel';
import { server } from '../mocks/server';

const inactiveStatus = {
  enabled: false,
  active: false,
  reachable: null,
  service_name: 'nyxgpt-api',
  otlp_endpoint: 'http://localhost:4318',
  jaeger_ui_url: 'http://localhost:16686',
  curated_views: [],
};

const activeStatusWithViews = {
  enabled: true,
  active: true,
  reachable: true,
  service_name: 'nyxgpt-api',
  otlp_endpoint: 'http://localhost:4318',
  jaeger_ui_url: 'http://localhost:16686',
  curated_views: [
    { label: 'RAG retrieval', hint: 'Traces for RAG document retrieval', url: 'http://localhost:16686/search?service=rag' },
  ],
};

const activeStatusNoViews = {
  enabled: true,
  active: true,
  reachable: true,
  service_name: 'nyxgpt-api',
  otlp_endpoint: 'http://localhost:4318',
  jaeger_ui_url: 'http://localhost:16686',
  curated_views: [],
};

const activeStatusUnreachable = {
  enabled: true,
  active: true,
  reachable: false,
  service_name: 'nyxgpt-api',
  otlp_endpoint: 'http://localhost:4318',
  jaeger_ui_url: 'http://localhost:16686',
  curated_views: [],
};

describe('TracingPanel', () => {
  it('shows a loading state before the status resolves', () => {
    server.use(http.get('/api/v1/tracing', () => new Promise(() => {})));

    render(<TracingPanel />);

    expect(screen.getByText(/Loading tracing status/i)).toBeInTheDocument();
  });

  it('shows setup guidance with the nyxgpt ops commands when inactive', async () => {
    server.use(http.get('/api/v1/tracing', () => HttpResponse.json(inactiveStatus)));

    render(<TracingPanel />);

    await waitFor(() => {
      expect(screen.getByText(/local-only/i)).toBeInTheDocument();
    });
    expect(screen.getByText('nyxgpt ops install')).toBeInTheDocument();
    expect(screen.getByText('nyxgpt ops observability')).toBeInTheDocument();
  });

  it('links to Jaeger and lists curated trace views when active', async () => {
    server.use(http.get('/api/v1/tracing', () => HttpResponse.json(activeStatusWithViews)));

    render(<TracingPanel />);

    const link = await screen.findByRole('link', { name: /Open Jaeger UI/i });
    expect(link).toHaveAttribute('href', 'http://localhost:16686');
    expect(screen.getByText('nyxgpt-api')).toBeInTheDocument();
    expect(screen.getByText('http://localhost:4318')).toBeInTheDocument();

    expect(screen.getByText('Curated trace views')).toBeInTheDocument();
    const viewLink = screen.getByRole('link', { name: /RAG retrieval/i });
    expect(viewLink).toHaveAttribute('href', 'http://localhost:16686/search?service=rag');
    expect(screen.getByText('Traces for RAG document retrieval')).toBeInTheDocument();
  });

  it('warns when active but the collector is unreachable (#3350)', async () => {
    server.use(http.get('/api/v1/tracing', () => HttpResponse.json(activeStatusUnreachable)));

    render(<TracingPanel />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/nothing is listening/i);
    expect(alert).toHaveTextContent(/silently dropped/i);
    // Still shows the regular active guidance (Jaeger link) alongside the warning.
    expect(await screen.findByRole('link', { name: /Open Jaeger UI/i })).toBeInTheDocument();
  });

  it('does not warn when active and reachable', async () => {
    server.use(http.get('/api/v1/tracing', () => HttpResponse.json(activeStatusWithViews)));

    render(<TracingPanel />);

    await screen.findByRole('link', { name: /Open Jaeger UI/i });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('omits the curated views section when active but none are configured', async () => {
    server.use(http.get('/api/v1/tracing', () => HttpResponse.json(activeStatusNoViews)));

    render(<TracingPanel />);

    await screen.findByRole('link', { name: /Open Jaeger UI/i });

    expect(screen.queryByText('Curated trace views')).not.toBeInTheDocument();
  });

  it('surfaces an error when the status response is not ok', async () => {
    server.use(http.get('/api/v1/tracing', () => new HttpResponse(null, { status: 500 })));

    render(<TracingPanel />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/HTTP 500/);
  });

  it('surfaces an error when the status request fails outright', async () => {
    server.use(http.get('/api/v1/tracing', () => HttpResponse.error()));

    render(<TracingPanel />);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('does not update state after unmounting before the status request rejects', async () => {
    let rejectRequest: ((reason: unknown) => void) | undefined;
    server.use(
      http.get(
        '/api/v1/tracing',
        () =>
          new Promise((_resolve, reject) => {
            rejectRequest = reject;
          })
      )
    );

    const { unmount } = render(<TracingPanel />);

    await vi.waitFor(() => {
      expect(rejectRequest).toBeDefined();
    });

    unmount();

    expect(() => {
      rejectRequest?.(new Error('network error'));
    }).not.toThrow();
  });

  it('does not update state after unmounting before the status request resolves', async () => {
    let resolveRequest: (() => void) | undefined;
    server.use(
      http.get(
        '/api/v1/tracing',
        () =>
          new Promise((resolve) => {
            resolveRequest = () => resolve(HttpResponse.json(activeStatusWithViews));
          })
      )
    );

    const { unmount } = render(<TracingPanel />);

    await vi.waitFor(() => {
      expect(resolveRequest).toBeDefined();
    });

    unmount();

    expect(() => {
      resolveRequest?.();
    }).not.toThrow();
  });

  it('reports a non-Error status failure with a stringified reason', async () => {
    const originalFetch = global.fetch;
    global.fetch = vi.fn().mockRejectedValue('plain string failure');

    render(<TracingPanel />);

    expect(await screen.findByRole('alert')).toHaveTextContent('plain string failure');

    global.fetch = originalFetch;
  });
});
