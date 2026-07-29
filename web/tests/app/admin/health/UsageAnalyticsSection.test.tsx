import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../../mocks/server';
import UsageAnalyticsSection from '../../../../src/app/admin/health/UsageAnalyticsSection';

const fullSummary = {
  total_requests: 120,
  total_prompt_tokens: 5001,
  total_completion_tokens: 3002,
  total_tokens: 8003,
  session_count: 12,
  by_model: [
    { model: 'llama3.1:8b', requests: 81, prompt_tokens: 3011, completion_tokens: 2021 },
    { model: 'mistral:7b', requests: 41, prompt_tokens: 2031, completion_tokens: 1041 },
  ],
  by_day: [
    { date: '2026-07-16', requests: 50, prompt_tokens: 2051, completion_tokens: 1061 },
    { date: '2026-07-17', requests: 70, prompt_tokens: 3071, completion_tokens: 2081 },
  ],
};

const emptySummary = {
  total_requests: 0,
  total_prompt_tokens: 0,
  total_completion_tokens: 0,
  total_tokens: 0,
  session_count: 0,
  by_model: [],
  by_day: [],
};

function mockUsage(summary = fullSummary) {
  server.use(http.get('/api/v1/analytics/usage', () => HttpResponse.json(summary)));
}

describe('UsageAnalyticsSection', () => {
  it('renders usage totals, model breakdown, and requests-by-day bars', async () => {
    mockUsage();

    render(<UsageAnalyticsSection />);

    await waitFor(() => {
      expect(screen.getByText('120')).toBeInTheDocument();
    });
    expect(screen.getByText('8,003')).toBeInTheDocument();
    expect(screen.getByText('5,001')).toBeInTheDocument();
    expect(screen.getByText('3,002')).toBeInTheDocument();
    expect(screen.getByText('12')).toBeInTheDocument();

    expect(screen.getByText('llama3.1:8b')).toBeInTheDocument();
    expect(screen.getByText('mistral:7b')).toBeInTheDocument();
    expect(screen.getByText('3,011')).toBeInTheDocument();
    expect(screen.getByText('1,041')).toBeInTheDocument();

    expect(screen.getByText('2026-07-16')).toBeInTheDocument();
    expect(screen.getByText('2026-07-17')).toBeInTheDocument();
  });

  it('shows the empty states for model usage and requests-by-day when there is no data', async () => {
    mockUsage(emptySummary);

    render(<UsageAnalyticsSection />);

    await waitFor(() => {
      expect(screen.getAllByText('No usage recorded yet.').length).toBe(2);
    });
  });

  it('shows an error and recovers on retry', async () => {
    server.use(http.get('/api/v1/analytics/usage', () => new HttpResponse(null, { status: 500 })));

    render(<UsageAnalyticsSection />);

    await waitFor(() => {
      expect(screen.getByText('Failed to load usage analytics')).toBeInTheDocument();
    });

    mockUsage(emptySummary);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /retry/i }));

    await waitFor(() => {
      expect(screen.getAllByText('No usage recorded yet.').length).toBe(2);
    });
  });

  it('shows the String(e) fallback message when loading rejects with a non-Error value', async () => {
    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('usage gremlin'));

    render(<UsageAnalyticsSection />);

    await waitFor(() => {
      expect(screen.getByText('usage gremlin')).toBeInTheDocument();
    });

    fetchSpy.mockRestore();
  });

  it('exports JSON using the server-provided filename', async () => {
    mockUsage();
    const user = userEvent.setup();

    render(<UsageAnalyticsSection />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /export json/i })).toBeInTheDocument();
    });

    server.use(
      http.get('/api/v1/analytics/export', () => {
        return new HttpResponse('{"events":[]}', {
          headers: {
            'Content-Type': 'application/json',
            'Content-Disposition': 'attachment; filename="usage_report_2026.json"',
          },
        });
      })
    );

    const createElementSpy = vi.spyOn(document, 'createElement');
    await user.click(screen.getByRole('button', { name: /export json/i }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /export json/i })).not.toBeDisabled();
    });
    const anchor = createElementSpy.mock.results.find((r) => r.value?.tagName === 'A')?.value as HTMLAnchorElement;
    expect(anchor.download).toBe('usage_report_2026.json');
    createElementSpy.mockRestore();
  });

  it('exports CSV using the default filename when Content-Disposition is absent', async () => {
    mockUsage();
    const user = userEvent.setup();

    render(<UsageAnalyticsSection />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /export csv/i })).toBeInTheDocument();
    });

    server.use(http.get('/api/v1/analytics/export', () => new HttpResponse('a,b\n1,2', { headers: { 'Content-Type': 'text/csv' } })));

    const createElementSpy = vi.spyOn(document, 'createElement');
    await user.click(screen.getByRole('button', { name: /export csv/i }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /export csv/i })).not.toBeDisabled();
    });
    const anchor = createElementSpy.mock.results.find((r) => r.value?.tagName === 'A')?.value as HTMLAnchorElement;
    expect(anchor.download).toBe('usage_report.csv');
    createElementSpy.mockRestore();
  });

  it('uses the default filename when Content-Disposition does not match the filename pattern', async () => {
    mockUsage();
    const user = userEvent.setup();

    render(<UsageAnalyticsSection />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /export json/i })).toBeInTheDocument();
    });

    server.use(
      http.get('/api/v1/analytics/export', () =>
        new HttpResponse('{}', { headers: { 'Content-Disposition': 'attachment' } })
      )
    );

    const createElementSpy = vi.spyOn(document, 'createElement');
    await user.click(screen.getByRole('button', { name: /export json/i }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /export json/i })).not.toBeDisabled();
    });
    const anchor = createElementSpy.mock.results.find((r) => r.value?.tagName === 'A')?.value as HTMLAnchorElement;
    expect(anchor.download).toBe('usage_report.json');
    createElementSpy.mockRestore();
  });

  it('walks every export error branch and allows retrying via the error banner', async () => {
    mockUsage();
    const user = userEvent.setup();

    render(<UsageAnalyticsSection />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /export json/i })).toBeInTheDocument();
    });

    server.use(http.get('/api/v1/analytics/export', () => new HttpResponse(null, { status: 500 })));
    await user.click(screen.getByRole('button', { name: /export json/i }));
    await waitFor(() => {
      expect(screen.getByText('Export failed')).toBeInTheDocument();
    });
    expect(screen.getByText(/Export failed: HTTP 500/)).toBeInTheDocument();

    const fetchSpy = vi.spyOn(global, 'fetch');
    fetchSpy.mockImplementationOnce(() => Promise.reject('export gremlin'));
    await user.click(screen.getByRole('button', { name: /export json/i }));
    await waitFor(() => {
      expect(screen.getByText('export gremlin')).toBeInTheDocument();
    });

    server.use(
      http.get('/api/v1/analytics/export', () =>
        new HttpResponse('{}', { headers: { 'Content-Disposition': 'attachment; filename="retry.json"' } })
      )
    );
    await user.click(screen.getByRole('button', { name: /^retry$/i }));
    await waitFor(() => {
      expect(screen.queryByText('Export failed')).not.toBeInTheDocument();
    });
    fetchSpy.mockRestore();
  });
});
