import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import '@testing-library/jest-dom';
import ResourceMetrics from '../../../src/app/settings/ResourceMetrics';

// Mock fetch
global.fetch = vi.fn() as any;

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

describe('ResourceMetrics', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders loading spinner initially', () => {
    (global.fetch as any).mockImplementation(() => new Promise(() => {}));

    render(<ResourceMetrics />);

    expect(screen.getByTestId('loading-spinner')).toBeInTheDocument();
  });

  it('fetches and displays metrics data', async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => mockMetricsData,
    });

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    expect(screen.getByText(/256\.5 MB/)).toBeInTheDocument();
    expect(screen.getByText(/15\.2%/)).toBeInTheDocument();
    expect(screen.getByText('CPU Utilization')).toBeInTheDocument();
    expect(screen.getByText(/5\.3%/)).toBeInTheDocument();
    expect(screen.getByText('Request Latency')).toBeInTheDocument();
    expect(screen.getByText(/45\.2 ms/)).toBeInTheDocument();
    expect(screen.getByText('Queue Status')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('1250')).toBeInTheDocument();
  });

  it('displays error message on fetch failure', async () => {
    (global.fetch as any).mockRejectedValueOnce(new Error('Network error'));

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText(/Network error/)).toBeInTheDocument();
    });
  });

  it('allows switching time ranges', async () => {
    (global.fetch as any).mockResolvedValue({
      ok: true,
      json: async () => mockMetricsData,
    });

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const dayButton = screen.getByRole('button', { name: /Last 24 Hours/i });
    fireEvent.click(dayButton);

    expect(dayButton).toHaveStyle({ background: 'var(--button)' });
  });

  it('auto-refreshes when enabled', async () => {
    (global.fetch as any).mockResolvedValue({
      ok: true,
      json: async () => mockMetricsData,
    });

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    expect(global.fetch).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(5000);

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledTimes(2);
    });
  });

  it('stops auto-refresh when disabled', async () => {
    (global.fetch as any).mockResolvedValue({
      ok: true,
      json: async () => mockMetricsData,
    });

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const checkbox = screen.getByRole('checkbox', { name: /Auto-refresh/i });
    fireEvent.click(checkbox);

    vi.advanceTimersByTime(5000);

    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it('exports data as CSV', async () => {
    (global.fetch as any).mockResolvedValue({
      ok: true,
      json: async () => mockMetricsData,
    });

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
    (global.fetch as any).mockResolvedValue({
      ok: true,
      json: async () => mockMetricsData,
    });

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

  it('displays warning colors for high memory usage', async () => {
    const highMemoryData = {
      ...mockMetricsData,
      memory: {
        ...mockMetricsData.memory,
        percent: 85.0,
      },
    };

    (global.fetch as any).mockResolvedValue({
      ok: true,
      json: async () => highMemoryData,
    });

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('Memory Usage')).toBeInTheDocument();
    });

    const memoryPercent = screen.getByText(/85\.0%/);
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

    (global.fetch as any).mockResolvedValue({
      ok: true,
      json: async () => highCpuData,
    });

    render(<ResourceMetrics />);

    await waitFor(() => {
      expect(screen.getByText('CPU Utilization')).toBeInTheDocument();
    });

    const cpuPercent = screen.getByText(/85\.0%/);
    expect(cpuPercent).toHaveStyle({ color: '#ef4444' });
  });
});
