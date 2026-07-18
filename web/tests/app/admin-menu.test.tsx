import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Home from '@/app/page';
import { ThemeProvider } from '@/contexts/ThemeContext';

// Home renders the session sidebar via next/dynamic(VirtualizedSessionList),
// which wraps react-virtuoso. Virtuoso relies on real layout/ResizeObserver
// measurements it can't get in happy-dom, so without this mock it renders
// zero items.
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ totalCount, itemContent, style, ...props }: any) => (
    <div style={style} aria-label={props['aria-label']} role={props.role}>
      {Array.from({ length: totalCount }).map((_, index) => (
        <div key={index}>{itemContent(index)}</div>
      ))}
    </div>
  ),
}));

vi.mock('@/contexts/ToastContext', () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() }),
  ToastProvider: ({ children }: { children: React.ReactNode }) => children,
}));

function renderHome() {
  return render(
    <ThemeProvider>
      <Home />
    </ThemeProvider>
  );
}

describe('Chat page Admin menu', () => {
  beforeEach(() => {
    global.fetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes('/api/sessions')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ sessions: [] }),
          text: () => Promise.resolve(JSON.stringify({ sessions: [] })),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
        text: () => Promise.resolve('{}'),
      });
    });
  });

  it('nests admin/ops links under a collapsible Admin group, collapsed by default', async () => {
    renderHome();

    const settingsButton = await screen.findByRole('button', { name: /settings/i });
    const user = userEvent.setup();
    await user.click(settingsButton);

    const adminToggle = await screen.findByRole('button', { name: /admin/i });
    expect(adminToggle).toHaveAttribute('aria-expanded', 'false');

    // Nested items aren't in the document until the group is expanded.
    expect(screen.queryByRole('link', { name: /dashboard/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /observability/i })).not.toBeInTheDocument();

    await user.click(adminToggle);
    expect(adminToggle).toHaveAttribute('aria-expanded', 'true');

    const links = await screen.findAllByRole('link');
    const hrefs = links.map((link) => link.getAttribute('href'));

    // Dashboard is the first nested item under Admin.
    const dashboardLink = screen.getByRole('link', { name: /dashboard/i });
    expect(dashboardLink).toHaveAttribute('href', '/admin/dashboard');

    // Observability links to the new submenu page.
    const observabilityLink = screen.getByRole('link', { name: /observability/i });
    expect(observabilityLink).toHaveAttribute('href', '/admin/observability');

    // The rest of the admin/ops surface is still reachable, nested under Admin.
    expect(hrefs).toEqual(
      expect.arrayContaining([
        '/admin',
        '/settings',
        '/admin/logs',
        '/admin/analytics',
        '/models',
        '/admin/collections',
        '/admin/playground',
        '/admin/deploy',
        '/admin/canary',
      ])
    );
  });

  it('keeps Theme as its own separate section outside the Admin group', async () => {
    renderHome();

    const settingsButton = await screen.findByRole('button', { name: /settings/i });
    const user = userEvent.setup();
    await user.click(settingsButton);

    // Theme options are visible without expanding Admin.
    expect(await screen.findByText('Theme')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /light/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /dark/i })).toBeInTheDocument();
  });

  it('does not close the Settings menu when toggling the Admin group', async () => {
    renderHome();

    const settingsButton = await screen.findByRole('button', { name: /settings/i });
    const user = userEvent.setup();
    await user.click(settingsButton);

    const adminToggle = await screen.findByRole('button', { name: /admin/i });
    await user.click(adminToggle);

    // The menu (and its Theme section) is still open, not collapsed.
    expect(await screen.findByText('Theme')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /light/i })).toBeInTheDocument();
    expect(adminToggle).toHaveAttribute('aria-expanded', 'true');
  });

  it('closes the Settings menu when clicking outside of it', async () => {
    renderHome();

    const settingsButton = await screen.findByRole('button', { name: /settings/i });
    const user = userEvent.setup();
    await user.click(settingsButton);

    expect(await screen.findByText('Theme')).toBeInTheDocument();

    await user.click(document.body);

    await waitFor(() => {
      expect(screen.queryByText('Theme')).not.toBeInTheDocument();
    });
  });

  it('closes the Settings menu when a submenu link is clicked', async () => {
    renderHome();

    const settingsButton = await screen.findByRole('button', { name: /settings/i });
    const user = userEvent.setup();
    await user.click(settingsButton);

    const adminToggle = await screen.findByRole('button', { name: /admin/i });
    await user.click(adminToggle);

    const dashboardLink = await screen.findByRole('link', { name: /dashboard/i });
    expect(dashboardLink).toHaveAttribute('href', '/admin/dashboard');
    await user.click(dashboardLink);

    await waitFor(() => {
      expect(screen.queryByText('Theme')).not.toBeInTheDocument();
    });
  });
});
