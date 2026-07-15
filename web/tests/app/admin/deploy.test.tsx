import { describe, it, expect } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import DeployPage from '../../../src/app/admin/deploy/page';

const mockStatus = {
  namespace: 'nyxgpt',
  active: 'blue',
  inactive: 'green',
  colors: {
    blue: { healthy: true, message: 'ok' },
    green: { healthy: true, message: 'ok' },
  },
  history: [],
  available: true,
  unavailable_reason: null,
};

describe('DeployPage', () => {
  it('renders the standardized back-nav link instead of a Back to Chat button', async () => {
    server.use(http.get('/api/v1/deploy/status', () => HttpResponse.json(mockStatus)));

    render(<DeployPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Blue/Green Deployment' })).toBeInTheDocument();
    });

    const link = screen.getByRole('link', { name: /back to admin dashboard/i });
    expect(link).toHaveAttribute('href', '/admin/dashboard');
    expect(screen.queryByRole('button', { name: /back to chat/i })).not.toBeInTheDocument();
  });
});
