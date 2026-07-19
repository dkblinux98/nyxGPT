import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import LogsPage from '../../../src/app/admin/logs/page';

const mockLogAggregationActive = {
  enabled: true,
  active: true,
  grafana_explore_url: 'http://localhost:3001/explore',
};

const mockLogAggregationDisabled = {
  enabled: false,
  active: false,
  grafana_explore_url: 'http://localhost:3001/explore',
};

const mockFiles = {
  files: [
    { name: 'nyxgpt.log', path: '/home/user/.nyxGPT/logs/nyxgpt.log', size: 1024, modified: 1768300000 },
    { name: 'api.log', path: '/home/user/.nyxGPT/logs/api.log', size: 2048, modified: 1768300100 },
  ],
};

const mockBackendInfo = {
  ollama_base_url: 'http://127.0.0.1:11434',
  default_model: 'llama3.1:8b',
  sessions_dir: '/home/user/.nyxGPT/sessions',
};

function mockLogAggregation(payload: object = mockLogAggregationDisabled) {
  server.use(http.get('/api/v1/log-aggregation', () => HttpResponse.json(payload)));
}

describe('LogsPage', () => {
  beforeEach(() => {
    mockLogAggregation();
    server.use(http.get('/api/v1/logs/files', () => HttpResponse.json(mockFiles)));
    server.use(
      http.get('/api/v1/logs/view/:filename', () =>
        HttpResponse.json({ lines: ['line one', 'line two'], total_lines: 2, filtered_lines: 2 })
      )
    );
    server.use(http.get('/api/info', () => HttpResponse.json(mockBackendInfo)));
  });

  it('renders the heading and back link', async () => {
    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Log Viewer' })).toBeInTheDocument();
    });
    const link = screen.getByRole('link', { name: /back to admin dashboard/i });
    expect(link).toHaveAttribute('href', '/admin/dashboard');
  });

  it('explains that this viewer only covers nyxGPT API logs, not Ollama/other components', async () => {
    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByText(/does not include logs from Ollama or other components/i)).toBeInTheDocument();
    });
  });

  it('renders the Log Aggregation panel with a Grafana Explore link when active', async () => {
    mockLogAggregation(mockLogAggregationActive);
    render(<LogsPage />);

    expect(await screen.findByText('Log Aggregation')).toBeInTheDocument();
    const grafanaLink = await screen.findByRole('link', { name: /Open Grafana Explore/i });
    expect(grafanaLink).toHaveAttribute('href', 'http://localhost:3001/explore');
  });

  it('renders the Log Aggregation panel disabled state', async () => {
    render(<LogsPage />);

    expect(await screen.findByText('Log Aggregation')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText(/searched centrally in Grafana/i)).toBeInTheDocument();
    });
  });

  it('shows the backend info panel once loaded', async () => {
    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByText('http://127.0.0.1:11434')).toBeInTheDocument();
    });
    expect(screen.getByText('llama3.1:8b')).toBeInTheDocument();
    expect(screen.getByText('/home/user/.nyxGPT/sessions')).toBeInTheDocument();
  });

  it('does not render the backend info panel when the request fails', async () => {
    server.use(http.get('/api/info', () => new HttpResponse(null, { status: 500 })));
    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Log Viewer' })).toBeInTheDocument();
    });
    expect(screen.queryByText('http://127.0.0.1:11434')).not.toBeInTheDocument();
  });

  it('logs an error to the console when the backend info request rejects', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const realFetch = global.fetch.bind(global);
    const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation((input, init) => {
      if (input === '/api/info') {
        return Promise.reject(new Error('backend info gremlin'));
      }
      return realFetch(input, init);
    });

    render(<LogsPage />);

    await waitFor(() => {
      expect(consoleSpy).toHaveBeenCalledWith('Failed to load backend info:', expect.any(Error));
    });
    fetchSpy.mockRestore();
    consoleSpy.mockRestore();
  });

  it('shows an empty state when no log files exist', async () => {
    server.use(http.get('/api/v1/logs/files', () => HttpResponse.json({ files: [] })));
    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByText('No log files found')).toBeInTheDocument();
    });
    expect(screen.getByText('Select a log file to view its contents')).toBeInTheDocument();
  });

  it('shows an empty state when the files response omits the files field', async () => {
    server.use(http.get('/api/v1/logs/files', () => HttpResponse.json({})));
    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByText('No log files found')).toBeInTheDocument();
    });
  });

  it('shows the empty-lines state when the view response omits the lines field', async () => {
    server.use(http.get('/api/v1/logs/view/:filename', () => HttpResponse.json({})));
    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByText('No log entries found')).toBeInTheDocument();
    });
  });

  it('shows an error and lets the user retry loading the file list', async () => {
    server.use(http.get('/api/v1/logs/files', () => new HttpResponse(null, { status: 500 })));
    const user = userEvent.setup();

    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByText('Failed to load log files')).toBeInTheDocument();
    });
    expect(screen.getByText('HTTP 500')).toBeInTheDocument();

    server.use(http.get('/api/v1/logs/files', () => HttpResponse.json(mockFiles)));
    await user.click(screen.getByRole('button', { name: /retry/i }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /nyxgpt\.log/ })).toBeInTheDocument();
    });
  });

  it('falls back to String(e) when the file-list request rejects with a non-Error value', async () => {
    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('files gremlin'));

    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByText('files gremlin')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('renders a zero-byte file size using the 0 B special case', async () => {
    server.use(
      http.get('/api/v1/logs/files', () =>
        HttpResponse.json({ files: [{ name: 'empty.log', path: '/x/empty.log', size: 0, modified: 1768300000 }] })
      )
    );

    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByText(/0 B/)).toBeInTheDocument();
    });
  });

  it('auto-selects the first file and shows its content, including the empty-lines state', async () => {
    server.use(http.get('/api/v1/logs/view/:filename', () => HttpResponse.json({ lines: [], total_lines: 0, filtered_lines: 0 })));

    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByText('No log entries found')).toBeInTheDocument();
    });
  });

  it('switches between files via the sidebar', async () => {
    render(<LogsPage />);
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByText('line one')).toBeInTheDocument();
    });

    server.use(
      http.get('/api/v1/logs/view/:filename', ({ params }) =>
        HttpResponse.json({ lines: [`content for ${params.filename}`], total_lines: 1, filtered_lines: 1 })
      )
    );
    await user.click(screen.getByRole('button', { name: /api\.log/ }));

    await waitFor(() => {
      expect(screen.getByText('content for api.log')).toBeInTheDocument();
    });
  });

  it('shows a content-loading error and allows retrying', async () => {
    render(<LogsPage />);
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByText('line one')).toBeInTheDocument();
    });

    server.use(http.get('/api/v1/logs/view/:filename', () => new HttpResponse(null, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /refresh/i }));

    await waitFor(() => {
      expect(screen.getByText('Failed to load log content')).toBeInTheDocument();
    });
    expect(screen.getByText('HTTP 500')).toBeInTheDocument();

    server.use(http.get('/api/v1/logs/view/:filename', () => HttpResponse.json({ lines: ['recovered'], total_lines: 1, filtered_lines: 1 })));
    await user.click(screen.getByRole('button', { name: /retry/i }));

    await waitFor(() => {
      expect(screen.getByText('recovered')).toBeInTheDocument();
    });
  });

  it('falls back to String(e) when the content request rejects with a non-Error value', async () => {
    render(<LogsPage />);
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByText('line one')).toBeInTheDocument();
    });

    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('content gremlin'));
    await user.click(screen.getByRole('button', { name: /refresh/i }));

    await waitFor(() => {
      expect(screen.getByText('content gremlin')).toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });

  it('filters by log level and search text, sending both as query params', async () => {
    render(<LogsPage />);
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByText('line one')).toBeInTheDocument();
    });

    let capturedUrl = '';
    server.use(
      http.get('/api/v1/logs/view/:filename', ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json({ lines: ['ERROR line'], total_lines: 1, filtered_lines: 1 });
      })
    );

    // Selecting a level fires an immediate (stale-state) fetch; the level param only
    // shows up on the next explicit reload once React has re-rendered with the new state.
    await user.selectOptions(screen.getByLabelText('Log Level'), 'ERROR');
    await user.click(screen.getByRole('button', { name: /refresh/i }));
    await waitFor(() => {
      expect(capturedUrl).toContain('level=ERROR');
    });

    await user.type(screen.getByLabelText('Search'), 'boom');
    await user.keyboard('{Enter}');
    await waitFor(() => {
      expect(capturedUrl).toContain('search=boom');
    });
  });

  it('changes the tail line count, including the blank-input fallback to 0', async () => {
    render(<LogsPage />);
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByText('line one')).toBeInTheDocument();
    });

    const tailInput = screen.getByLabelText('Tail Lines') as HTMLInputElement;
    await user.clear(tailInput);
    await user.type(tailInput, '50');
    expect(tailInput).toHaveValue(50);

    await user.clear(tailInput);
    expect(tailInput).toHaveValue(0);

    let capturedUrl = '';
    server.use(
      http.get('/api/v1/logs/view/:filename', ({ request }) => {
        capturedUrl = request.url;
        return HttpResponse.json({ lines: ['all lines'], total_lines: 1, filtered_lines: 1 });
      })
    );
    await user.click(screen.getByRole('button', { name: /refresh/i }));
    await waitFor(() => {
      expect(capturedUrl).not.toContain('tail=');
    });
  });

  it('downloads the selected file in a new tab', async () => {
    render(<LogsPage />);
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByText('line one')).toBeInTheDocument();
    });

    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    await user.click(screen.getByRole('button', { name: /download/i }));
    expect(openSpy).toHaveBeenCalledWith('/api/v1/logs/stream/nyxgpt.log', '_blank');
    openSpy.mockRestore();
  });

  it('auto-scrolls the log container once content settles', async () => {
    render(<LogsPage />);
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByText('line one')).toBeInTheDocument();
    });

    const scrollToSpy = vi.spyOn(HTMLElement.prototype, 'scrollTo').mockImplementation(() => {});
    await user.click(screen.getByRole('button', { name: /refresh/i }));

    await waitFor(
      () => {
        expect(scrollToSpy).toHaveBeenCalledWith(
          expect.objectContaining({ behavior: 'smooth' })
        );
      },
      { timeout: 1000 }
    );
    scrollToSpy.mockRestore();
  });

  it('toggles auto-scroll off via its checkbox', async () => {
    render(<LogsPage />);
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByText('line one')).toBeInTheDocument();
    });

    // This checkbox is bound to a ref (not state) so unchecking it doesn't trigger a
    // re-render -- just exercise the onChange handler that flips the ref's value.
    const autoScrollCheckbox = screen.getByRole('checkbox', { name: /auto-scroll/i });
    expect(autoScrollCheckbox).toBeChecked();
    await user.click(autoScrollCheckbox);
  });

  describe('auto-refresh polling', () => {
    beforeEach(() => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it('polls for new content on the configured interval, and stops when disabled', async () => {
      let viewCalls = 0;
      server.use(
        http.get('/api/v1/logs/view/:filename', () => {
          viewCalls += 1;
          return HttpResponse.json({ lines: [`poll ${viewCalls}`], total_lines: 1, filtered_lines: 1 });
        })
      );

      render(<LogsPage />);

      await waitFor(() => {
        expect(screen.getByText('poll 1')).toBeInTheDocument();
      });

      // The interval input is hidden until auto-refresh is enabled; enable it first.
      const autoRefreshCheckbox = screen.getByRole('checkbox', { name: /^auto-refresh$/i });
      fireEvent.click(autoRefreshCheckbox);
      expect(autoRefreshCheckbox).toBeChecked();

      fireEvent.change(screen.getByPlaceholderText('Seconds'), { target: { value: '1' } });

      vi.advanceTimersByTime(1000);
      await waitFor(() => {
        expect(viewCalls).toBeGreaterThanOrEqual(2);
      });

      const callsBeforeDisable = viewCalls;
      fireEvent.click(autoRefreshCheckbox);
      expect(autoRefreshCheckbox).not.toBeChecked();

      vi.advanceTimersByTime(5000);
      expect(viewCalls).toBe(callsBeforeDisable);
    });

    it('falls back to a 5 second interval when the input is cleared', async () => {
      let viewCalls = 0;
      server.use(
        http.get('/api/v1/logs/view/:filename', () => {
          viewCalls += 1;
          return HttpResponse.json({ lines: [`poll ${viewCalls}`], total_lines: 1, filtered_lines: 1 });
        })
      );

      render(<LogsPage />);

      await waitFor(() => {
        expect(screen.getByText('poll 1')).toBeInTheDocument();
      });

      const autoRefreshCheckbox = screen.getByRole('checkbox', { name: /^auto-refresh$/i });
      fireEvent.click(autoRefreshCheckbox);

      const intervalInput = screen.getByPlaceholderText('Seconds');
      fireEvent.change(intervalInput, { target: { value: '' } });
      expect(intervalInput).toHaveValue(5);

      vi.advanceTimersByTime(5000);
      await waitFor(() => {
        expect(viewCalls).toBeGreaterThanOrEqual(2);
      });
    });
  });
});
