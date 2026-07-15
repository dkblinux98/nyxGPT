import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import GeneralSettings from '../../../src/app/settings/GeneralSettings';
import { ThemeProvider } from '../../../src/contexts/ThemeContext';

const mockInfo = {
  ollama_base_url: 'http://localhost:11434',
  default_model: 'llama3',
  sessions_dir: '/home/user/.nyxGPT/sessions',
  release_version: 'v2.0.0',
};

function renderWithTheme() {
  return render(
    <ThemeProvider>
      <GeneralSettings />
    </ThemeProvider>
  );
}

describe('GeneralSettings', () => {
  beforeEach(() => {
    // Re-assign after tests/setup.ts's MSW server.listen() patches global.fetch,
    // so this mock isn't clobbered by MSW's real fetch interceptor.
    global.fetch = vi.fn() as any;
    vi.clearAllMocks();
    localStorage.clear();
  });

  it('renders the appearance section with theme controls', async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => mockInfo,
    });

    renderWithTheme();

    expect(screen.getByText('Appearance')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Light/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Dark/i })).toBeInTheDocument();
  });

  it('switches theme when a theme button is clicked', async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => mockInfo,
    });

    renderWithTheme();

    const darkButton = screen.getByRole('button', { name: /Dark/i });
    fireEvent.click(darkButton);

    await waitFor(() => {
      expect(darkButton).toHaveAttribute('aria-pressed', 'true');
      expect(localStorage.getItem('theme')).toBe('dark');
    });

    const lightButton = screen.getByRole('button', { name: /Light/i });
    fireEvent.click(lightButton);

    await waitFor(() => {
      expect(lightButton).toHaveAttribute('aria-pressed', 'true');
      expect(localStorage.getItem('theme')).toBe('light');
    });
  });

  it('fetches and displays app info in the about section', async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => mockInfo,
    });

    renderWithTheme();

    await waitFor(() => {
      expect(screen.getByText('About')).toBeInTheDocument();
    });

    await waitFor(() => {
      expect(screen.getByText('v2.0.0')).toBeInTheDocument();
    });

    expect(screen.getByText('llama3')).toBeInTheDocument();
    expect(screen.getByText('http://localhost:11434')).toBeInTheDocument();
    expect(screen.getByText('/home/user/.nyxGPT/sessions')).toBeInTheDocument();
  });

  it('displays an error message when fetching app info fails', async () => {
    (global.fetch as any).mockRejectedValueOnce(new Error('Network error'));

    renderWithTheme();

    await waitFor(() => {
      expect(screen.getByText(/Network error/)).toBeInTheDocument();
    });
  });

  it('links to the Configuration Wizard for advanced settings', async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => mockInfo,
    });

    renderWithTheme();

    const link = screen.getByRole('link', { name: /Configuration Wizard/i });
    expect(link).toHaveAttribute('href', '/admin');
  });
});
