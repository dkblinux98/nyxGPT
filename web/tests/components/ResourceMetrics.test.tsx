import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import '@testing-library/jest-dom';
import ResourceMetrics from '../../src/components/ResourceMetrics';
import { server } from '../mocks/server';

const mockMetricsData = {
  memory: { rss_mb: 256.5, vms_mb: 512.0, percent: 15.2, available_mb: 8192.0 },
  cpu: { process_percent: 5.3, system_percent: 25.8 },
  latency: { avg_ms: 45.2, p50_ms: 42.0, p95_ms: 85.5, p99_ms: 120.3 },
  queue: { depth: 3, total_requests: 1250 },
};

describe('ResourceMetrics (admin dashboard)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows a loading spinner before the first fetch resolves', () => {
    server.use(http.get('/api/v1/metrics', () => new Promise(() => {})));

    render(<ResourceMetrics />);

    expect(screen.getByRole('status')).toBeInTheDocument();
  });

  it('renders memory, CPU, latency, and queue metrics once loaded', async () => {
    server.use(http.get('/api/v1/metrics', () => HttpResponse.json(mockMetricsData)));

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    expect(screen.getByText('15.2%')).toBeInTheDocument();
    expect(screen.getByText('256.5 MB')).toBeInTheDocument();
    expect(screen.getByText('512.0 MB')).toBeInTheDocument();
    expect(screen.getByText('8192 MB')).toBeInTheDocument();

    expect(screen.getByText('CPU Usage')).toBeInTheDocument();
    expect(screen.getByText('5.3%')).toBeInTheDocument();
    expect(screen.getByText('25.8%')).toBeInTheDocument();

    expect(screen.getByText('Request Latency')).toBeInTheDocument();
    expect(screen.getByText('85.5ms')).toBeInTheDocument();
    expect(screen.getByText('45.2 ms')).toBeInTheDocument();
    expect(screen.getByText('42.0 ms')).toBeInTheDocument();
    expect(screen.getByText('120.3 ms')).toBeInTheDocument();

    expect(screen.getByText('Queue Status')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('1,250')).toBeInTheDocument();
  });

  it('shows a retryable error message when the metrics response is not ok', async () => {
    server.use(http.get('/api/v1/metrics', () => new HttpResponse(null, { status: 500 })));

    render(<ResourceMetrics />);

    expect(await screen.findByText('Failed to load resource metrics')).toBeInTheDocument();
    expect(screen.getByText(/HTTP 500/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument();
  });

  it('shows an error message when the metrics request fails outright', async () => {
    server.use(http.get('/api/v1/metrics', () => HttpResponse.error()));

    render(<ResourceMetrics />);

    expect(await screen.findByText('Failed to load resource metrics')).toBeInTheDocument();
  });

  it('reports a non-Error metrics failure with a stringified reason', async () => {
    const originalFetch = global.fetch;
    global.fetch = vi.fn().mockRejectedValue('plain string failure');

    render(<ResourceMetrics />);

    expect(await screen.findByText(/plain string failure/)).toBeInTheDocument();

    global.fetch = originalFetch;
  });

  it('renders nothing when the metrics response resolves to no data', async () => {
    server.use(http.get('/api/v1/metrics', () => HttpResponse.json(null)));

    const { container } = render(<ResourceMetrics />);

    await waitFor(() => {
      expect(container).toBeEmptyDOMElement();
    });
  });

  it('retries loading metrics when the retry button is clicked', async () => {
    let callCount = 0;
    server.use(
      http.get('/api/v1/metrics', () => {
        callCount += 1;
        if (callCount === 1) {
          return new HttpResponse(null, { status: 500 });
        }
        return HttpResponse.json(mockMetricsData);
      })
    );

    render(<ResourceMetrics />);

    const retryButton = await screen.findByRole('button', { name: /Retry/i });
    fireEvent.click(retryButton);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });
  });

  it('shows normal-level colors for healthy metrics', async () => {
    server.use(http.get('/api/v1/metrics', () => HttpResponse.json(mockMetricsData)));

    render(<ResourceMetrics />);

    const memoryPercent = await screen.findByText('15.2%');
    expect(memoryPercent).toHaveStyle({ color: '#44ff44' });
  });

  it('shows warning-level colors for elevated memory, CPU, and latency', async () => {
    const warningData = {
      memory: { rss_mb: 256.5, vms_mb: 512.0, percent: 70, available_mb: 8192.0 },
      cpu: { process_percent: 65, system_percent: 25.8 },
      latency: { avg_ms: 45.2, p50_ms: 42.0, p95_ms: 600, p99_ms: 120.3 },
      queue: { depth: 3, total_requests: 1250 },
    };
    server.use(http.get('/api/v1/metrics', () => HttpResponse.json(warningData)));

    render(<ResourceMetrics />);

    const memoryPercent = await screen.findByText('70.0%');
    expect(memoryPercent).toHaveStyle({ color: '#ffaa00' });

    const cpuPercent = screen.getByText('65.0%');
    expect(cpuPercent).toHaveStyle({ color: '#ffaa00' });

    const latencyValue = screen.getByText('600.0ms');
    expect(latencyValue).toHaveStyle({ color: '#ffaa00' });
  });

  it('shows critical-level colors for severe memory, CPU, and latency', async () => {
    const criticalData = {
      memory: { rss_mb: 256.5, vms_mb: 512.0, percent: 95, available_mb: 8192.0 },
      cpu: { process_percent: 90, system_percent: 25.8 },
      latency: { avg_ms: 45.2, p50_ms: 42.0, p95_ms: 1500, p99_ms: 120.3 },
      queue: { depth: 3, total_requests: 1250 },
    };
    server.use(http.get('/api/v1/metrics', () => HttpResponse.json(criticalData)));

    render(<ResourceMetrics />);

    const memoryPercent = await screen.findByText('95.0%');
    expect(memoryPercent).toHaveStyle({ color: '#ff4444' });

    const cpuPercent = screen.getByText('90.0%');
    expect(cpuPercent).toHaveStyle({ color: '#ff4444' });

    const latencyValue = screen.getByText('1500.0ms');
    expect(latencyValue).toHaveStyle({ color: '#ff4444' });
  });

  it('manually refreshes metrics when the refresh button is clicked', async () => {
    let callCount = 0;
    server.use(
      http.get('/api/v1/metrics', () => {
        callCount += 1;
        return HttpResponse.json(mockMetricsData);
      })
    );

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });
    expect(callCount).toBe(1);

    const refreshButton = screen.getByRole('button', { name: /^Refresh$/i });
    fireEvent.click(refreshButton);

    await waitFor(() => {
      expect(callCount).toBe(2);
    });
  });

  it('shows a disabled, refreshing state on the refresh button while a manual refresh is in flight', async () => {
    let resolveSecondFetch: (() => void) | undefined;
    let callCount = 0;
    server.use(
      http.get('/api/v1/metrics', () => {
        callCount += 1;
        if (callCount === 1) {
          return HttpResponse.json(mockMetricsData);
        }
        return new Promise((resolve) => {
          resolveSecondFetch = () => resolve(HttpResponse.json(mockMetricsData));
        });
      })
    );

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const refreshButton = screen.getByRole('button', { name: /^Refresh$/i });
    fireEvent.click(refreshButton);

    await waitFor(() => {
      expect(resolveSecondFetch).toBeDefined();
    });

    const refreshingButton = screen.getByRole('button', { name: /Refreshing/i });
    expect(refreshingButton).toBeDisabled();
    expect(refreshingButton.style.opacity).toBe('0.6');
    expect(refreshingButton.style.cursor).toBe('not-allowed');

    resolveSecondFetch?.();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /^Refresh$/i })).not.toBeDisabled();
    });
  });

  it('auto-refreshes every 5 seconds while enabled', async () => {
    let callCount = 0;
    server.use(
      http.get('/api/v1/metrics', () => {
        callCount += 1;
        return HttpResponse.json(mockMetricsData);
      })
    );

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });
    expect(callCount).toBe(1);

    vi.advanceTimersByTime(5000);

    await waitFor(() => {
      expect(callCount).toBe(2);
    });
  });

  it('stops auto-refreshing once disabled via the checkbox', async () => {
    let callCount = 0;
    server.use(
      http.get('/api/v1/metrics', () => {
        callCount += 1;
        return HttpResponse.json(mockMetricsData);
      })
    );

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const checkbox = screen.getByRole('checkbox', { name: /Auto-refresh/i });
    fireEvent.click(checkbox);
    expect(checkbox).not.toBeChecked();

    const callsAfterDisable = callCount;
    vi.advanceTimersByTime(10000);

    expect(callCount).toBe(callsAfterDisable);
  });

  it('exports metrics as JSON', async () => {
    server.use(http.get('/api/v1/metrics', () => HttpResponse.json(mockMetricsData)));

    const createObjectURLSpy = vi.spyOn(URL, 'createObjectURL').mockReturnValue('mock-blob-url');
    const revokeObjectURLSpy = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const exportButton = screen.getByRole('button', { name: /Export JSON/i });
    fireEvent.click(exportButton);

    expect(createObjectURLSpy).toHaveBeenCalled();
    expect(revokeObjectURLSpy).toHaveBeenCalledWith('mock-blob-url');

    createObjectURLSpy.mockRestore();
    revokeObjectURLSpy.mockRestore();
  });

  it('exports metrics as CSV', async () => {
    server.use(http.get('/api/v1/metrics', () => HttpResponse.json(mockMetricsData)));

    const createObjectURLSpy = vi.spyOn(URL, 'createObjectURL').mockReturnValue('mock-blob-url');
    const revokeObjectURLSpy = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const exportButton = screen.getByRole('button', { name: /Export CSV/i });
    fireEvent.click(exportButton);

    expect(createObjectURLSpy).toHaveBeenCalled();
    expect(revokeObjectURLSpy).toHaveBeenCalledWith('mock-blob-url');

    createObjectURLSpy.mockRestore();
    revokeObjectURLSpy.mockRestore();
  });

  it('shows a link to the Observability page instead of embedding the dashboards inline', async () => {
    server.use(http.get('/api/v1/metrics', () => HttpResponse.json(mockMetricsData)));

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Observability')).toBeInTheDocument();
    });

    const link = screen.getByRole('link', { name: /Open Observability/i });
    expect(link).toHaveAttribute('href', '/admin/observability');

    // The Grafana/Prometheus/Jaeger/GlitchTip panels were promoted out of
    // this component onto the Observability page -- they should not render
    // (and their status endpoints should not be fetched) here anymore.
    expect(screen.queryByText('Monitoring Dashboards')).not.toBeInTheDocument();
    expect(screen.queryByText('Distributed Tracing')).not.toBeInTheDocument();
    expect(screen.queryByText('Error Tracking')).not.toBeInTheDocument();
    expect(screen.queryByText('Log Aggregation')).not.toBeInTheDocument();
    expect(screen.queryByText('Prometheus Endpoint')).not.toBeInTheDocument();
  });
});
