import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import '@testing-library/jest-dom';
import QueryCacheStatsPanel from '../../src/components/QueryCacheStatsPanel';
import { server } from '../mocks/server';

const enabledStats = {
  hits: 8,
  misses: 2,
  hit_rate: 0.8,
  size: 3,
  enabled: true,
  backend: 'memory',
  max_size: 500,
  ttl_seconds: 300,
  rag_enabled: true,
};

const disabledStats = {
  hits: 0,
  misses: 0,
  hit_rate: 0.0,
  size: 0,
  enabled: false,
  backend: 'none',
  max_size: null,
  ttl_seconds: null,
  rag_enabled: false,
};

const ragDisabledStats = {
  hits: 0,
  misses: 0,
  hit_rate: 0.0,
  size: 0,
  enabled: true,
  backend: 'memory',
  max_size: 500,
  ttl_seconds: 300,
  rag_enabled: false,
};

describe('QueryCacheStatsPanel', () => {
  beforeEach(() => {
    global.confirm = vi.fn().mockReturnValue(true);
  });

  it('shows a loading state before the stats resolve', () => {
    server.use(http.get('/api/v1/rag/cache/stats', () => new Promise(() => {})));

    render(<QueryCacheStatsPanel />);

    expect(screen.getByText(/Loading query cache stats/i)).toBeInTheDocument();
  });

  it('renders populated stats once loaded', async () => {
    server.use(http.get('/api/v1/rag/cache/stats', () => HttpResponse.json(enabledStats)));

    render(<QueryCacheStatsPanel />);

    await waitFor(() => {
      expect(screen.getByText('80.0%')).toBeInTheDocument();
    });
    expect(screen.getByText('8')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.getByText('3 / 500')).toBeInTheDocument();
    expect(screen.getByText('memory')).toBeInTheDocument();
    expect(screen.getByText('300s')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /clear cache/i })).toBeInTheDocument();
  });

  it('renders size without a max when the backend has no size limit', async () => {
    server.use(
      http.get('/api/v1/rag/cache/stats', () =>
        HttpResponse.json({ ...enabledStats, backend: 'disk', max_size: null, ttl_seconds: 600 })
      )
    );

    render(<QueryCacheStatsPanel />);

    await waitFor(() => {
      expect(screen.getByText('3')).toBeInTheDocument();
    });
    expect(screen.getByText('disk')).toBeInTheDocument();
    expect(screen.getByText('600s')).toBeInTheDocument();
  });

  it('shows "n/a" for TTL when an enabled cache reports no configured TTL', async () => {
    server.use(
      http.get('/api/v1/rag/cache/stats', () => HttpResponse.json({ ...enabledStats, ttl_seconds: null }))
    );

    render(<QueryCacheStatsPanel />);

    await waitFor(() => {
      expect(screen.getByText('n/a')).toBeInTheDocument();
    });
  });

  it('shows a clear message instead of an error when caching is disabled', async () => {
    server.use(http.get('/api/v1/rag/cache/stats', () => HttpResponse.json(disabledStats)));

    render(<QueryCacheStatsPanel />);

    await waitFor(() => {
      expect(screen.getByText(/Query result caching is disabled/i)).toBeInTheDocument();
    });
    expect(screen.getByText('query_cache_enabled')).toBeInTheDocument();
    const wizardLink = screen.getByRole('link', { name: /configuration wizard/i });
    expect(wizardLink).toHaveAttribute('href', '/admin');
    expect(screen.queryByRole('button', { name: /clear cache/i })).not.toBeInTheDocument();
    expect(screen.queryByText('80.0%')).not.toBeInTheDocument();
    expect(screen.queryByText('Hit rate')).not.toBeInTheDocument();
  });

  it('explains zeroed stats instead of showing bare zeros when RAG is disabled', async () => {
    server.use(http.get('/api/v1/rag/cache/stats', () => HttpResponse.json(ragDisabledStats)));

    render(<QueryCacheStatsPanel />);

    await waitFor(() => {
      expect(screen.getByText(/RAG is disabled globally/i)).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: /clear cache/i })).not.toBeInTheDocument();
    expect(screen.queryByText('Hit rate')).not.toBeInTheDocument();
    expect(screen.queryByText('0.0%')).not.toBeInTheDocument();
    // Not the cache-disabled message -- this is a distinct, non-error state.
    expect(screen.queryByText(/Query result caching is disabled/i)).not.toBeInTheDocument();
  });

  it('surfaces an error when the stats request fails', async () => {
    server.use(http.get('/api/v1/rag/cache/stats', () => new HttpResponse(null, { status: 500 })));

    render(<QueryCacheStatsPanel />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/HTTP 500/);
  });

  it('clears the cache after confirmation and refreshes stats', async () => {
    server.use(http.get('/api/v1/rag/cache/stats', () => HttpResponse.json(enabledStats)));
    const user = userEvent.setup();

    render(<QueryCacheStatsPanel />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /clear cache/i })).toBeInTheDocument();
    });

    server.use(
      http.post('/api/v1/rag/cache/clear', () =>
        HttpResponse.json({ status: 'Query result cache cleared' })
      ),
      http.get('/api/v1/rag/cache/stats', () =>
        HttpResponse.json({ ...enabledStats, hits: 0, misses: 0, hit_rate: 0, size: 0 })
      )
    );

    await user.click(screen.getByRole('button', { name: /clear cache/i }));

    expect(global.confirm).toHaveBeenCalled();
    await waitFor(() => {
      expect(screen.getByText('Query result cache cleared')).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.getByText('0.0%')).toBeInTheDocument();
    });
  });

  it('falls back to a default message when the clear response has no status', async () => {
    server.use(http.get('/api/v1/rag/cache/stats', () => HttpResponse.json(enabledStats)));
    const user = userEvent.setup();

    render(<QueryCacheStatsPanel />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /clear cache/i })).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/rag/cache/clear', () => HttpResponse.json({})));

    await user.click(screen.getByRole('button', { name: /clear cache/i }));

    await waitFor(() => {
      expect(screen.getByText('Query result cache cleared')).toBeInTheDocument();
    });
  });

  it('does not clear the cache when the confirmation is declined', async () => {
    server.use(http.get('/api/v1/rag/cache/stats', () => HttpResponse.json(enabledStats)));
    global.confirm = vi.fn().mockReturnValue(false);
    const user = userEvent.setup();

    render(<QueryCacheStatsPanel />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /clear cache/i })).toBeInTheDocument();
    });

    const seenPosts: string[] = [];
    const listener = ({ request }: { request: Request }) => {
      if (request.url.includes('/api/v1/rag/cache/clear')) {
        seenPosts.push(request.url);
      }
    };
    server.events.on('request:start', listener);

    await user.click(screen.getByRole('button', { name: /clear cache/i }));

    expect(seenPosts).toHaveLength(0);
    expect(screen.queryByText('Query result cache cleared')).not.toBeInTheDocument();
    server.events.removeListener('request:start', listener);
  });

  it('shows an error when clearing the cache fails', async () => {
    server.use(http.get('/api/v1/rag/cache/stats', () => HttpResponse.json(enabledStats)));
    const user = userEvent.setup();

    render(<QueryCacheStatsPanel />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /clear cache/i })).toBeInTheDocument();
    });

    server.use(
      http.post('/api/v1/rag/cache/clear', () =>
        HttpResponse.json({ detail: 'Cache backend unavailable' }, { status: 500 })
      )
    );

    await user.click(screen.getByRole('button', { name: /clear cache/i }));

    await waitFor(() => {
      expect(screen.getByText('Cache backend unavailable')).toBeInTheDocument();
    });
  });

  it('falls back to an HTTP status message when the clear error has no detail', async () => {
    server.use(http.get('/api/v1/rag/cache/stats', () => HttpResponse.json(enabledStats)));
    const user = userEvent.setup();

    render(<QueryCacheStatsPanel />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /clear cache/i })).toBeInTheDocument();
    });

    server.use(http.post('/api/v1/rag/cache/clear', () => new HttpResponse(null, { status: 503 })));

    await user.click(screen.getByRole('button', { name: /clear cache/i }));

    await waitFor(() => {
      expect(screen.getByText(/HTTP 503/)).toBeInTheDocument();
    });
  });

  it('reports a non-Error stats failure with a stringified reason', async () => {
    const originalFetch = global.fetch;
    global.fetch = vi.fn().mockRejectedValue('plain string failure');

    render(<QueryCacheStatsPanel />);

    expect(await screen.findByRole('alert')).toHaveTextContent('plain string failure');

    global.fetch = originalFetch;
  });

  it('reports a non-Error clear failure with a stringified reason', async () => {
    server.use(http.get('/api/v1/rag/cache/stats', () => HttpResponse.json(enabledStats)));
    const user = userEvent.setup();

    render(<QueryCacheStatsPanel />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /clear cache/i })).toBeInTheDocument();
    });

    const realFetch = global.fetch;
    const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation((input, init) => {
      const url = typeof input === 'string' ? input : (input as Request).url;
      if (url.includes('/api/v1/rag/cache/clear')) {
        return Promise.reject('clear gremlin');
      }
      return realFetch(input, init);
    });

    await user.click(screen.getByRole('button', { name: /clear cache/i }));

    await waitFor(() => {
      expect(screen.getByText('clear gremlin')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });
});
