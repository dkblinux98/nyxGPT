/**
 * Tests for the chat page's Settings -> Support group (#3745, #3811).
 *
 * The group holds exactly two items and both are links into this app: Docs,
 * and File an Issue. Neither asks a question and neither consults the
 * backend -- the intake page is where the filer is asked what kind of ticket
 * this is, and where the install's ability to file is worked out.
 *
 * That is the fix for the acceptance failure this file used to encode. The
 * previous menu asked the ticket type first (three entries, one per type)
 * and then decided from a runtime probe -- `can_submit` on
 * `/api/v1/support/context` -- whether the entry opened an in-app dialog or
 * navigated to github.com's compose page. Every way that probe could come
 * back short ended on github.com, which is the one destination the spec
 * rules out, and the type the filer had already chosen arrived there as
 * `None`. So the tests below pin the *absence* of all of it: no type entries
 * in the menu, no external link, and no context request at all.
 */
import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Home from '@/app/page';
import { ThemeProvider } from '@/contexts/ThemeContext';

type VirtuosoProps = {
  totalCount: number;
  itemContent: (index: number) => React.ReactNode;
  style?: React.CSSProperties;
  'aria-label'?: string;
  role?: string;
};

// Same reason as admin-menu.test.tsx: react-virtuoso relies on real layout and
// ResizeObserver measurements it can't get in happy-dom, so the session
// sidebar renders zero items without this.
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ totalCount, itemContent, style, ...props }: VirtuosoProps) => (
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

/** Stubs every request the chat page makes. */
function stubFetch() {
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
}

function renderHome() {
  return render(
    <ThemeProvider>
      <Home />
    </ThemeProvider>
  );
}

async function openSupportGroup() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: /settings/i }));
  const supportToggle = await screen.findByRole('button', { name: /support/i });
  await user.click(supportToggle);
  return { user, supportToggle };
}

function supportContextCalls() {
  return (global.fetch as ReturnType<typeof vi.fn>).mock.calls.filter(([url]) =>
    String(url).includes('/api/v1/support/context')
  );
}

describe('Chat page Support menu', () => {
  beforeEach(() => {
    stubFetch();
  });

  it('nests Docs and File an Issue under a collapsible Support group, collapsed by default', async () => {
    renderHome();

    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /settings/i }));

    const supportToggle = await screen.findByRole('button', { name: /support/i });
    expect(supportToggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('link', { name: /docs/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /file an issue/i })).not.toBeInTheDocument();

    await user.click(supportToggle);
    expect(supportToggle).toHaveAttribute('aria-expanded', 'true');

    expect(screen.getByRole('link', { name: /docs/i })).toHaveAttribute('href', '/support/docs');
    expect(screen.getByRole('link', { name: /file an issue/i })).toHaveAttribute(
      'href',
      '/support/new'
    );
  });

  it('offers exactly two items, both inside nyxGPT, and never asks the ticket type here', async () => {
    // The re-tested failure, pinned from the outside: the menu showed one
    // entry per ticket type and each was an anchor to github.com, so a filer
    // answered the type question twice and never reached the product's own
    // intake. Both halves are asserted absent, by shape rather than by name,
    // so a differently-worded reintroduction still trips this.
    renderHome();
    await openSupportGroup();

    const group = screen.getByRole('group', { name: 'Support' });
    const links = Array.from(group.querySelectorAll('a'));
    expect(links).toHaveLength(2);
    for (const link of links) {
      const href = link.getAttribute('href') ?? '';
      expect(href.startsWith('/')).toBe(true);
      expect(href).not.toContain('github.com');
      expect(link).not.toHaveAttribute('target');
    }
    // No buttons either: a ticket-type entry was a button on an install that
    // could file, and a link on one that could not.
    expect(group.querySelectorAll('button')).toHaveLength(0);
    for (const ticketType of ['Bug Found', 'Feature Request', 'Question']) {
      expect(screen.queryByText(ticketType)).not.toBeInTheDocument();
    }
  });

  it('links File an Issue at a route this app actually serves', async () => {
    // The href and the page are written in two different files, and a link
    // to a route that does not exist is a 404 the menu cannot detect on its
    // own -- the failure being fixed here started as exactly that kind of
    // silent mismatch. So the rendered href is resolved back to the page
    // file Next.js would serve it from.
    renderHome();
    await openSupportGroup();

    const href = screen.getByRole('link', { name: /file an issue/i }).getAttribute('href');
    // `node:path` rather than `new URL(..., import.meta.url)`: happy-dom
    // replaces the global URL and its relative resolution does not survive
    // the round trip.
    const here = dirname(fileURLToPath(import.meta.url));
    expect(existsSync(resolve(here, '../../src/app', `.${href}`, 'page.tsx'))).toBe(true);
  });

  it('opens the Support group without asking the backend anything', async () => {
    // The menu holds no state that could route it somewhere else. Opening it
    // used to fetch `/api/v1/support/context` and decide from the answer;
    // that probe, and every degraded path it had, is gone.
    renderHome();
    await openSupportGroup();

    await screen.findByRole('link', { name: /file an issue/i });
    expect(supportContextCalls()).toHaveLength(0);
  });

  it('highlights each Support item on hover and clears it on leave', async () => {
    renderHome();

    await openSupportGroup();
    const docs = screen.getByRole('link', { name: /docs/i });
    const issue = screen.getByRole('link', { name: /file an issue/i });

    for (const item of [docs, issue] as HTMLElement[]) {
      fireEvent.mouseEnter(item);
      expect(item.style.background).toBe('var(--button-hover)');
      // happy-dom refuses to overwrite a `var()` background shorthand with a
      // keyword, so clear it first -- otherwise the leave handler's write is
      // invisible here even though it runs (and works in a browser).
      item.style.background = '';
      fireEvent.mouseLeave(item);
      expect(item.style.background).toBe('transparent');
    }
  });

  it('does not close the Settings menu when toggling the Support group', async () => {
    renderHome();

    const { supportToggle } = await openSupportGroup();

    expect(await screen.findByText('Theme')).toBeInTheDocument();
    expect(supportToggle).toHaveAttribute('aria-expanded', 'true');
  });

  it('closes the Settings menu when a Support item is clicked', async () => {
    renderHome();

    const { user } = await openSupportGroup();
    await user.click(screen.getByRole('link', { name: /docs/i }));

    await waitFor(() => {
      expect(screen.queryByText('Theme')).not.toBeInTheDocument();
    });
  });

  it('closes the Settings menu when File an Issue is clicked', async () => {
    // The menu has to get out of the way: the intake is a page, and a menu
    // left standing would cover the top of it after the navigation.
    renderHome();

    const { user } = await openSupportGroup();
    await user.click(screen.getByRole('link', { name: /file an issue/i }));

    await waitFor(() => {
      expect(screen.queryByText('Theme')).not.toBeInTheDocument();
    });
  });

  it('collapses the Support group when the menu is closed from outside or with Escape', async () => {
    renderHome();

    const { user, supportToggle } = await openSupportGroup();
    expect(supportToggle).toHaveAttribute('aria-expanded', 'true');

    await user.click(document.body);
    await waitFor(() => expect(screen.queryByText('Theme')).not.toBeInTheDocument());

    // Reopening shows the group collapsed again, not left open from before.
    await user.click(await screen.findByRole('button', { name: /settings/i }));
    expect(await screen.findByRole('button', { name: /support/i })).toHaveAttribute(
      'aria-expanded',
      'false'
    );

    await user.click(screen.getByRole('button', { name: /support/i }));
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByText('Theme')).not.toBeInTheDocument());

    await user.click(await screen.findByRole('button', { name: /settings/i }));
    expect(await screen.findByRole('button', { name: /support/i })).toHaveAttribute(
      'aria-expanded',
      'false'
    );
  });
});
