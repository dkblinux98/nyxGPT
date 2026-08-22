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
    global.fetch = vi.fn() as unknown as typeof fetch;
    vi.clearAllMocks();
    localStorage.clear();
  });

  it('renders the appearance section with theme controls', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => mockInfo,
    });

    renderWithTheme();

    expect(screen.getByText('Appearance')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Light/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Dark/i })).toBeInTheDocument();
  });

  it('switches theme when a theme button is clicked', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce({
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
    vi.mocked(global.fetch).mockResolvedValueOnce({
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
    vi.mocked(global.fetch).mockRejectedValueOnce(new Error('Network error'));

    renderWithTheme();

    await waitFor(() => {
      expect(screen.getByText(/Network error/)).toBeInTheDocument();
    });
  });

  it('reports a non-Error app info failure with a stringified reason', async () => {
    vi.mocked(global.fetch).mockRejectedValueOnce('plain string failure');

    renderWithTheme();

    await waitFor(() => {
      expect(screen.getByText('plain string failure')).toBeInTheDocument();
    });
  });

  it('displays an error message when the app info response is not ok', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce({
      ok: false,
      status: 500,
    });

    renderWithTheme();

    await waitFor(() => {
      expect(screen.getByText(/Failed to fetch app info: HTTP 500/)).toBeInTheDocument();
    });
  });

  it('shows a fallback version label when release_version is not set', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ ...mockInfo, release_version: null }),
    });

    renderWithTheme();

    // Two rows read "unknown" since #3982 -- the API's version and the web
    // tier's -- because the About surface now names both tiers separately.
    await waitFor(() => {
      expect(screen.getAllByText('unknown').length).toBeGreaterThan(0);
    });
  });

  /**
   * #3982: the About surface reported one version for two separately
   * installed services. An operator asking "what am I running?" got the
   * API's number and no way to see that the page asking the question came
   * from a different build.
   */
  describe('stack tiers (#3982)', () => {
    it('names the API and web versions as separate rows', async () => {
      vi.mocked(global.fetch).mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          ...mockInfo,
          release_version: '3.0.0rc13',
          web_version: '3.0.0rc13',
          web_version_source: 'homebrew-keg',
        }),
      });

      renderWithTheme();

      await waitFor(() => {
        expect(screen.getByText('API Version')).toBeInTheDocument();
      });
      expect(screen.getByText('Web UI Version')).toBeInTheDocument();
      // rc suffix intact, tier named: the two questions the header could
      // not answer during acceptance.
      expect(screen.getAllByText('v3.0.0rc13 (rc)')).toHaveLength(2);
      expect(screen.getByText('from the installed Homebrew keg')).toBeInTheDocument();
    });

    it('warns when the two tiers are different builds', async () => {
      vi.mocked(global.fetch).mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          ...mockInfo,
          release_version: '3.0.0',
          web_version: '2.1.0',
          web_version_source: 'homebrew-keg',
        }),
      });

      renderWithTheme();

      const warning = await screen.findByTestId('settings-version-mismatch');
      expect(warning).toHaveTextContent('web v2.1.0');
      expect(warning).toHaveTextContent('API v3.0.0');
    });

    it('does not warn on a matched stack', async () => {
      vi.mocked(global.fetch).mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          ...mockInfo,
          release_version: '3.0.0',
          web_version: '3.0.0',
          web_version_source: 'homebrew-keg',
        }),
      });

      renderWithTheme();

      await waitFor(() => {
        expect(screen.getByText('API Version')).toBeInTheDocument();
      });
      expect(screen.queryByTestId('settings-version-mismatch')).not.toBeInTheDocument();
    });
  });

  it('does not update state after unmounting before the app info request resolves', async () => {
    let resolveRequest: (() => void) | undefined;
    vi.mocked(global.fetch).mockImplementationOnce(
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
    vi.mocked(global.fetch).mockImplementationOnce(
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
    vi.mocked(global.fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => mockInfo,
    });

    renderWithTheme();

    const link = screen.getByRole('link', { name: /Configuration Wizard/i });
    expect(link).toHaveAttribute('href', '/admin');
  });
});
