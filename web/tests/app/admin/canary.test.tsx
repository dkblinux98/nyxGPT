import { describe, it, expect } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import CanaryPage from '../../../src/app/admin/canary/page';

const mockStatus = {
  namespace: 'nyxgpt',
  active: false,
  weight_percent: 0,
  stable: { healthy: true, message: 'ok' },
  canary: { healthy: true, message: 'ok' },
  metrics: { total_requests: 0, error_rate_percent: 0, p95_latency_ms: 0 },
  history: [],
  available: true,
  unavailable_reason: null,
};

describe('CanaryPage', () => {
  it('renders the standardized back-nav link instead of a Back to Chat button', async () => {
    server.use(http.get('/api/v1/canary/status', () => HttpResponse.json(mockStatus)));

    render(<CanaryPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Canary Deployment' })).toBeInTheDocument();
    });

    const link = screen.getByRole('link', { name: /back to admin dashboard/i });
    expect(link).toHaveAttribute('href', '/admin/dashboard');
    expect(screen.queryByRole('button', { name: /back to chat/i })).not.toBeInTheDocument();
  });
});
