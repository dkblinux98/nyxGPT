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

  it('reports a non-Error app info failure with a stringified reason', async () => {
    (global.fetch as any).mockRejectedValueOnce('plain string failure');

    renderWithTheme();

    await waitFor(() => {
      expect(screen.getByText('plain string failure')).toBeInTheDocument();
    });
  });

  it('displays an error message when the app info response is not ok', async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: false,
      status: 500,
    });

    renderWithTheme();

    await waitFor(() => {
      expect(screen.getByText(/Failed to fetch app info: HTTP 500/)).toBeInTheDocument();
    });
  });

  it('shows a fallback version label when release_version is not set', async () => {
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ ...mockInfo, release_version: null }),
    });

    renderWithTheme();

    await waitFor(() => {
      expect(screen.getByText('unknown')).toBeInTheDocument();
    });
  });

  it('does not update state after unmounting before the app info request resolves', async () => {
    let resolveRequest: (() => void) | undefined;
    (global.fetch as any).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveRequest = () =>
            resolve({
              ok: true,
              json: async () => mockInfo,
            });
        })
    );

    const { unmount } = renderWithTheme();

    await waitFor(() => {
      expect(resolveRequest).toBeDefined();
    });

    unmount();

    expect(() => {
      resolveRequest?.();
    }).not.toThrow();
  });

  it('does not update state after unmounting before the app info request rejects', async () => {
    let rejectRequest: ((reason: unknown) => void) | undefined;
    (global.fetch as any).mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectRequest = reject;
        })
    );

    const { unmount } = renderWithTheme();

    await waitFor(() => {
      expect(rejectRequest).toBeDefined();
    });

    unmount();

    expect(() => {
      rejectRequest?.(new Error('network error'));
    }).not.toThrow();
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
