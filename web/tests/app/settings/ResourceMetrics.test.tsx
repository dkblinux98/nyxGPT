import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import '@testing-library/jest-dom';
import ResourceMetrics from '../../../src/app/settings/ResourceMetrics';

const mockMetricsData = {
  memory: {
    rss_mb: 256.5,
    vms_mb: 512.0,
    percent: 15.2,
    available_mb: 8192.0,
  },
  cpu: {
    process_percent: 5.3,
    system_percent: 25.8,
  },
  latency: {
    avg_ms: 45.2,
    p50_ms: 42.0,
    p95_ms: 85.5,
    p99_ms: 120.3,
  },
  queue: {
    depth: 3,
    total_requests: 1250,
  },
};

function emptyHistoryResponse(overrides: Record<string, unknown> = {}) {
  return {
    range: '1h',
    points: [],
    sample_interval_seconds: 60,
    requested_window_seconds: 3600,
    earliest_available_ts: null,
    history_available_seconds: 0,
    ...overrides,
  };
}

function jsonResponse(body: unknown, init: { ok?: boolean; status?: number } = {}) {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: async () => body,
  };
}

// The component now hits two endpoints -- the current-snapshot /api/metrics
// (still 5s-polled for the "current" tiles) and the server-side history
// series at /api/v1/metrics/history?range=... -- so the fetch mock has to
// route by URL instead of returning one canned response for every call.
function mockFetchRouting(
  overrides: {
    metrics?: () => Promise<unknown>;
    history?: () => Promise<unknown>;
  } = {}
) {
  const metrics = overrides.metrics ?? (() => Promise.resolve(jsonResponse(mockMetricsData)));
  const history = overrides.history ?? (() => Promise.resolve(jsonResponse(emptyHistoryResponse())));
  return vi.fn((url: unknown) => {
    if (typeof url === 'string' && url.startsWith('/api/v1/metrics/history')) {
      return history();
    }
    return metrics();
  });
}

function countCalls(fetchMock: ReturnType<typeof vi.fn>, matcher: (url: string) => boolean): number {
  return fetchMock.mock.calls.filter(([url]) => typeof url === 'string' && matcher(url)).length;
}

describe('ResourceMetrics', () => {
  beforeEach(() => {
    // Re-assign after tests/setup.ts's MSW server.listen() patches global.fetch,
    // so this mock isn't clobbered by MSW's real fetch interceptor.
    global.fetch = mockFetchRouting() as unknown as typeof fetch;
    vi.clearAllMocks();
    // shouldAdvanceTime keeps real time passing (so testing-library's waitFor,
    // which doesn't recognize vitest's fake timers, still resolves) while still
    // letting tests fast-forward the 5s/60s auto-refresh intervals manually.
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders loading spinner initially', () => {
    global.fetch = vi.fn(() => new Promise(() => {})) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    expect(screen.getByRole('status')).toBeInTheDocument();
  });

  it('fetches and displays metrics data', async () => {
    global.fetch = mockFetchRouting() as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    expect(screen.getByText('256.5 MB')).toBeInTheDocument();
    expect(screen.getByText('15.2%')).toBeInTheDocument();
    expect(screen.getByText('CPU Utilization')).toBeInTheDocument();
    expect(screen.getByText('5.3%')).toBeInTheDocument();
    expect(screen.getByText('Request Latency')).toBeInTheDocument();
    expect(screen.getByText(/45\.2 ms/)).toBeInTheDocument();
    expect(screen.getByText('Queue Status')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('1250')).toBeInTheDocument();
  });

  it('displays error message on fetch failure', async () => {
    global.fetch = mockFetchRouting({
      metrics: () => Promise.reject(new Error('Network error')),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText(/Network error/)).toBeInTheDocument();
    });
  });

  it('displays an error message when the metrics response is not ok', async () => {
    global.fetch = mockFetchRouting({
      metrics: () => Promise.resolve(jsonResponse(null, { ok: false, status: 500 })),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText(/Failed to fetch metrics: HTTP 500/)).toBeInTheDocument();
    });
  });

  it('reports a non-Error metrics failure with a stringified reason', async () => {
    global.fetch = mockFetchRouting({
      metrics: () => Promise.reject('plain string failure'),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('plain string failure')).toBeInTheDocument();
    });
  });

  it('shows a fallback message when no metrics data is available', async () => {
    global.fetch = mockFetchRouting({
      metrics: () => Promise.resolve(jsonResponse(null)),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('No metrics data available')).toBeInTheDocument();
    });
  });

  it('fetches server-side history for the selected range and re-fetches on range switch', async () => {
    const fetchMock = mockFetchRouting();
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('/api/v1/metrics/history?range=1h');
    });

    const dayButton = screen.getByRole('button', { name: /Last 24 Hours/i });
    fireEvent.click(dayButton);

    // toHaveStyle can't resolve CSS custom properties in happy-dom, so
    // compare the inline style value directly.
    expect(dayButton.style.background).toBe('var(--button)');

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('/api/v1/metrics/history?range=24h');
    });

    const weekButton = screen.getByRole('button', { name: /Last 7 Days/i });
    fireEvent.click(weekButton);

    expect(weekButton.style.background).toBe('var(--button)');

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('/api/v1/metrics/history?range=7d');
    });

    const hourButton = screen.getByRole('button', { name: /Last Hour/i });
    fireEvent.click(hourButton);

    expect(hourButton.style.background).toBe('var(--button)');

    // Switching back to a previously-visited range re-fetches rather than
    // reusing a stale cached response, since server-side history can have
    // grown in the meantime.
    await waitFor(() => {
      expect(countCalls(fetchMock, (u) => u.startsWith('/api/v1/metrics/history?range=1h'))).toBe(2);
    });
  });

  it('auto-refreshes the current snapshot every 5 seconds when enabled', async () => {
    const fetchMock = mockFetchRouting();
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const before = countCalls(fetchMock, (u) => u === '/api/metrics');
    expect(before).toBe(1);

    vi.advanceTimersByTime(5000);

    await waitFor(() => {
      expect(countCalls(fetchMock, (u) => u === '/api/metrics')).toBe(before + 1);
    });
  });

  it('periodically refreshes server-side history while auto-refresh is enabled', async () => {
    const fetchMock = mockFetchRouting();
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const before = countCalls(fetchMock, (u) => u.startsWith('/api/v1/metrics/history'));
    expect(before).toBe(1);

    await vi.advanceTimersByTimeAsync(60000);

    await waitFor(() => {
      expect(countCalls(fetchMock, (u) => u.startsWith('/api/v1/metrics/history'))).toBe(before + 1);
    });
  });

  it('stops auto-refresh (both snapshot and history polling) when disabled', async () => {
    const fetchMock = mockFetchRouting();
    global.fetch = fetchMock as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const checkbox = screen.getByRole('checkbox', { name: /Auto-refresh/i });
    fireEvent.click(checkbox);

    // Toggling auto-refresh re-runs both fetch effects once more (an
    // immediate manual refresh of the snapshot and the history), but must
    // not schedule new intervals.
    await waitFor(() => {
      expect(countCalls(fetchMock, (u) => u === '/api/metrics')).toBe(2);
      expect(countCalls(fetchMock, (u) => u.startsWith('/api/v1/metrics/history'))).toBe(2);
    });

    const metricsAfterToggle = countCalls(fetchMock, (u) => u === '/api/metrics');
    const historyAfterToggle = countCalls(fetchMock, (u) => u.startsWith('/api/v1/metrics/history'));

    vi.advanceTimersByTime(65000);

    expect(countCalls(fetchMock, (u) => u === '/api/metrics')).toBe(metricsAfterToggle);
    expect(countCalls(fetchMock, (u) => u.startsWith('/api/v1/metrics/history'))).toBe(historyAfterToggle);
  });

  it('renders only the most recent 20 points of the selected range', async () => {
    const nowSeconds = Math.floor(Date.now() / 1000);
    const points = Array.from({ length: 25 }, (_, i) => ({
      ts: nowSeconds - (25 - i) * 60,
      memory_rss_mb: 100 + i,
      memory_percent: 10 + i,
      cpu_process_percent: 5,
      cpu_system_percent: 20,
      avg_latency_ms: 30,
      p99_latency_ms: 90,
      queue_depth: 1,
    }));

    global.fetch = mockFetchRouting({
      history: () =>
        Promise.resolve(
          jsonResponse(
            emptyHistoryResponse({
              points,
              earliest_available_ts: points[0].ts,
              history_available_seconds: 3600,
            })
          )
        ),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    await waitFor(() => {
      const memoryTrendHeading = screen.getByText('Memory Trend');
      const memoryTrendLines = memoryTrendHeading.nextElementSibling;
      expect(memoryTrendLines?.children.length).toBe(20);
    });
  });

  it('shows an honest message when less history exists than the requested window', async () => {
    const nowSeconds = Math.floor(Date.now() / 1000);
    global.fetch = mockFetchRouting({
      history: () =>
        Promise.resolve(
          jsonResponse(
            emptyHistoryResponse({
              points: [
                {
                  ts: nowSeconds - 120,
                  memory_rss_mb: 100,
                  memory_percent: 10,
                  cpu_process_percent: 5,
                  cpu_system_percent: 20,
                  avg_latency_ms: 30,
                  p99_latency_ms: 90,
                  queue_depth: 1,
                },
              ],
              earliest_available_ts: nowSeconds - 120,
              history_available_seconds: 120,
            })
          )
        ),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText(/Only 2m of history available/)).toBeInTheDocument();
    });
  });

  it('shows an error message when the history response is not ok', async () => {
    global.fetch = mockFetchRouting({
      history: () => Promise.resolve(jsonResponse(null, { ok: false, status: 500 })),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText(/Failed to load history: Failed to fetch metrics history: HTTP 500/)).toBeInTheDocument();
    });
  });

  it('shows the full window duration in days when enough history exists (formatDuration days branch)', async () => {
    global.fetch = mockFetchRouting({
      history: () =>
        Promise.resolve(
          jsonResponse(
            emptyHistoryResponse({
              range: '7d',
              requested_window_seconds: 604800,
              history_available_seconds: 604800,
            })
          )
        ),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const weekButton = screen.getByRole('button', { name: /Last 7 Days/i });
    fireEvent.click(weekButton);

    await waitFor(() => {
      expect(screen.getByText(/Showing the full 7\.0d window/)).toBeInTheDocument();
    });
  });

  it('reports a non-Error history failure with a stringified reason', async () => {
    global.fetch = mockFetchRouting({
      history: () => Promise.reject('plain string history failure'),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText(/Failed to load history: plain string history failure/)).toBeInTheDocument();
    });
  });

  it('exports history_available_seconds as 0 when history has not loaded yet', async () => {
    global.fetch = mockFetchRouting({
      history: () => new Promise(() => {}), // never resolves
    }) as unknown as typeof fetch;

    let capturedBlob: Blob | null = null;
    global.URL.createObjectURL = vi.fn((blob: Blob) => {
      capturedBlob = blob;
      return 'blob:mock';
    }) as unknown as typeof URL.createObjectURL;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const exportButton = screen.getByRole('button', { name: /Export JSON/i });
    fireEvent.click(exportButton);

    expect(capturedBlob).not.toBeNull();
    const text = await capturedBlob!.text();
    expect(JSON.parse(text).history_available_seconds).toBe(0);
  });

  it('shows an empty-history message when no samples exist yet', async () => {
    global.fetch = mockFetchRouting({
      history: () => Promise.resolve(jsonResponse(emptyHistoryResponse())),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    // Turn off auto-refresh so the live 5s snapshot isn't appended as a
    // transient point -- this isolates the "server has no history yet" case.
    const checkbox = screen.getByRole('checkbox', { name: /Auto-refresh/i });
    fireEvent.click(checkbox);

    await waitFor(() => {
      expect(screen.getByText(/No history yet for this window/)).toBeInTheDocument();
    });
  });

  it('exports data as CSV', async () => {
    global.fetch = mockFetchRouting() as unknown as typeof fetch;

    const mockCreateObjectURL = vi.fn();
    global.URL.createObjectURL = mockCreateObjectURL;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const exportButton = screen.getByRole('button', { name: /Export CSV/i });
    fireEvent.click(exportButton);

    expect(mockCreateObjectURL).toHaveBeenCalled();
  });

  it('exports data as JSON', async () => {
    global.fetch = mockFetchRouting() as unknown as typeof fetch;

    const mockCreateObjectURL = vi.fn();
    global.URL.createObjectURL = mockCreateObjectURL;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const exportButton = screen.getByRole('button', { name: /Export JSON/i });
    fireEvent.click(exportButton);

    expect(mockCreateObjectURL).toHaveBeenCalled();
  });

  it('changes export button backgrounds on hover', async () => {
    global.fetch = mockFetchRouting() as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const csvButton = screen.getByRole('button', { name: /Export CSV/i });
    fireEvent.mouseEnter(csvButton);
    expect(csvButton.style.background).toBe('var(--button-hover)');
    fireEvent.mouseLeave(csvButton);
    expect(csvButton.style.background).toBe('var(--button)');

    const jsonButton = screen.getByRole('button', { name: /Export JSON/i });
    fireEvent.mouseEnter(jsonButton);
    expect(jsonButton.style.background).toBe('var(--button-hover)');
    fireEvent.mouseLeave(jsonButton);
    expect(jsonButton.style.background).toBe('var(--button)');
  });

  it('displays warning colors for high memory usage', async () => {
    const highMemoryData = {
      ...mockMetricsData,
      memory: {
        ...mockMetricsData.memory,
        percent: 85.0,
      },
    };

    global.fetch = mockFetchRouting({
      metrics: () => Promise.resolve(jsonResponse(highMemoryData)),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const memoryPercent = screen.getByText('85.0%');
    expect(memoryPercent).toHaveStyle({ color: '#f59e0b' });
  });

  it('displays critical colors for very high CPU usage', async () => {
    const highCpuData = {
      ...mockMetricsData,
      cpu: {
        process_percent: 85.0,
        system_percent: 25.8,
      },
    };

    global.fetch = mockFetchRouting({
      metrics: () => Promise.resolve(jsonResponse(highCpuData)),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('CPU Utilization')).toBeInTheDocument();
    });

    const cpuPercent = screen.getByText('85.0%');
    expect(cpuPercent).toHaveStyle({ color: '#ef4444' });
  });

  it('displays critical colors for very high memory usage', async () => {
    const criticalMemoryData = {
      ...mockMetricsData,
      memory: { ...mockMetricsData.memory, percent: 95.0 },
    };

    global.fetch = mockFetchRouting({
      metrics: () => Promise.resolve(jsonResponse(criticalMemoryData)),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const memoryPercent = screen.getByText('95.0%');
    expect(memoryPercent).toHaveStyle({ color: '#ef4444' });
  });

  it('displays warning colors for elevated CPU process usage', async () => {
    const warningCpuData = {
      ...mockMetricsData,
      cpu: { process_percent: 70.0, system_percent: 25.8 },
    };

    global.fetch = mockFetchRouting({
      metrics: () => Promise.resolve(jsonResponse(warningCpuData)),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('CPU Utilization')).toBeInTheDocument();
    });

    const cpuPercent = screen.getByText('70.0%');
    expect(cpuPercent).toHaveStyle({ color: '#f59e0b' });
  });

  it('displays warning colors for elevated system CPU usage', async () => {
    const warningSystemCpuData = {
      ...mockMetricsData,
      cpu: { process_percent: 5.3, system_percent: 80.0 },
    };

    global.fetch = mockFetchRouting({
      metrics: () => Promise.resolve(jsonResponse(warningSystemCpuData)),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('CPU Utilization')).toBeInTheDocument();
    });

    const systemPercent = screen.getByText('80.0%');
    expect(systemPercent).toHaveStyle({ color: '#f59e0b' });
  });

  it('displays critical colors for very high system CPU usage', async () => {
    const criticalSystemCpuData = {
      ...mockMetricsData,
      cpu: { process_percent: 5.3, system_percent: 95.0 },
    };

    global.fetch = mockFetchRouting({
      metrics: () => Promise.resolve(jsonResponse(criticalSystemCpuData)),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('CPU Utilization')).toBeInTheDocument();
    });

    const systemPercent = screen.getByText('95.0%');
    expect(systemPercent).toHaveStyle({ color: '#ef4444' });
  });

  it('displays warning colors for elevated P99 latency', async () => {
    const warningLatencyData = {
      ...mockMetricsData,
      latency: { ...mockMetricsData.latency, p99_ms: 700.0 },
    };

    global.fetch = mockFetchRouting({
      metrics: () => Promise.resolve(jsonResponse(warningLatencyData)),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Request Latency')).toBeInTheDocument();
    });

    const p99 = screen.getByText(/700\.0 ms/);
    expect(p99).toHaveStyle({ color: '#f59e0b' });
  });

  it('displays critical colors for very high P99 latency', async () => {
    const criticalLatencyData = {
      ...mockMetricsData,
      latency: { ...mockMetricsData.latency, p99_ms: 1500.0 },
    };

    global.fetch = mockFetchRouting({
      metrics: () => Promise.resolve(jsonResponse(criticalLatencyData)),
    }) as unknown as typeof fetch;

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Request Latency')).toBeInTheDocument();
    });

    const p99 = screen.getByText(/1500\.0 ms/);
    expect(p99).toHaveStyle({ color: '#ef4444' });
  });
});
