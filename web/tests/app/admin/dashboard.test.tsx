import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import AdminDashboardPage from '../../../src/app/admin/dashboard/page';

describe('AdminDashboardPage', () => {
  beforeEach(() => {
    global.confirm = vi.fn().mockReturnValue(true);
  });

  it('renders the dashboard heading and back link', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Admin Dashboard' })).toBeInTheDocument();
    });
    const link = screen.getByRole('link', { name: /back to chat/i });
    expect(link).toHaveAttribute('href', '/');
  });

  it('renders system status overview after loading', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText(/Deploy: blue/)).toBeInTheDocument();
    });
    expect(screen.getByText(/Canary: idle/)).toBeInTheDocument();
    expect(screen.getByText(/Auth: disabled/)).toBeInTheDocument();
  });

  it('renders configuration summary with a link to the wizard', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getAllByText('llama3.1:8b').length).toBeGreaterThan(0);
    });
    const wizardLink = screen.getByRole('link', { name: /open configuration wizard/i });
    expect(wizardLink).toHaveAttribute('href', '/admin');
  });

  it('renders the activity log with recent events', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText('config.updated')).toBeInTheDocument();
    });
    expect(screen.getByText('deploy.switch')).toBeInTheDocument();
  });

  it('renders access management with masked key state', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText(/API key authentication disabled/)).toBeInTheDocument();
    });
    expect(screen.getByText('not set')).toBeInTheDocument();
  });

  it('toggles auth enabled when the checkbox is clicked', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText(/API key authentication disabled/)).toBeInTheDocument();
    });

    const checkbox = screen.getByRole('checkbox');
    fireEvent.click(checkbox);

    await waitFor(() => {
      expect(screen.getByText(/API key authentication enabled/)).toBeInTheDocument();
    });
  });

  it('reveals the new API key once after rotation', async () => {
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /rotate api key/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /rotate api key/i }));

    await waitFor(() => {
      expect(screen.getByText('newly-generated-key-value')).toBeInTheDocument();
    });
    expect(global.confirm).toHaveBeenCalled();
  });

  it('does not rotate the key when the confirmation is declined', async () => {
    global.confirm = vi.fn().mockReturnValue(false);
    render(<AdminDashboardPage />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /rotate api key/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /rotate api key/i }));

    expect(screen.queryByText('newly-generated-key-value')).not.toBeInTheDocument();
  });
});
