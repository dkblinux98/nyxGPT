/**
 * Tests for the chat page's Settings -> Support group (#3745, #3811).
 *
 * Docs and File an Issue behave differently on purpose: Docs is packaged with
 * the install and always works, while filing needs the network, so the links
 * are resolved lazily (only when the group is opened) and nothing usable is
 * offered until that resolves. These tests pin both halves, including the
 * silent-failure path -- a broken context fetch must leave the item disabled
 * rather than open a dead GitHub URL.
 *
 * Filing is one entry per ticket type (#3811). The filer classifies the
 * ticket here, in nyxGPT, because nothing downstream would otherwise record
 * it: the Support project types tickets with a project field and GitHub maps
 * a form answer to neither a label nor a field.
 */
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

const ISSUE_URL = 'https://github.com/dkblinux8/nyxGPT/issues/new?template=support.yml';
const NETWORK_NOTE = 'Filing an issue opens GitHub in your browser and needs internet access.';

/** What `/api/v1/support/context` reports for the three ticket types. */
const TICKET_TYPES = [
  {
    value: 'Bug Found',
    description: 'Something is broken or behaving wrongly',
    url: `${ISSUE_URL}&ticket_type=Bug+Found`,
  },
  {
    value: 'Feature Request',
    description: 'Something nyxGPT should be able to do',
    url: `${ISSUE_URL}&ticket_type=Feature+Request`,
  },
  {
    value: 'Question',
    description: 'How do I …? / Is this supposed to …?',
    url: `${ISSUE_URL}&ticket_type=Question`,
  },
];

const CONTEXT_BODY = {
  issue_form_url: ISSUE_URL,
  ticket_types: TICKET_TYPES,
  network_note: NETWORK_NOTE,
};

/**
 * Stubs every request the chat page makes, with `/api/v1/support/context`
 * answered however the test needs.
 */
function stubFetch(supportContext: () => Promise<unknown>) {
  global.fetch = vi.fn().mockImplementation((url: string) => {
    if (url.includes('/api/v1/support/context')) return supportContext();
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

function okContext() {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(CONTEXT_BODY),
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
    stubFetch(okContext);
  });

  it('nests Docs and File an Issue under a collapsible Support group, collapsed by default', async () => {
    renderHome();

    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /settings/i }));

    const supportToggle = await screen.findByRole('button', { name: /support/i });
    expect(supportToggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('link', { name: /docs/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /bug found/i })).not.toBeInTheDocument();

    await user.click(supportToggle);
    expect(supportToggle).toHaveAttribute('aria-expanded', 'true');

    expect(screen.getByRole('link', { name: /docs/i })).toHaveAttribute('href', '/support/docs');

    // Docs plus one filing entry per ticket type (#3811), each carrying its
    // own prefilled URL -- the type the filer picked here is the type the
    // form arrives with.
    await waitFor(() =>
      expect(screen.getByRole('group', { name: 'Support' }).querySelectorAll('a')).toHaveLength(
        1 + TICKET_TYPES.length
      )
    );
    for (const ticketType of TICKET_TYPES) {
      // Regex, not an exact name: each entry renders its emoji alongside the
      // label, so the accessible name is "🐛 Bug Found".
      expect(screen.getByRole('link', { name: new RegExp(ticketType.value, 'i') })).toHaveAttribute(
        'href',
        ticketType.url
      );
    }
  });

  it('does not fetch the issue link until the group is opened, and fetches it only once', async () => {
    renderHome();

    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /settings/i }));
    // Docs work offline; an install that never opens Support should pay
    // nothing for a link it may never use.
    expect(supportContextCalls()).toHaveLength(0);

    const supportToggle = await screen.findByRole('button', { name: /support/i });
    await user.click(supportToggle);
    await waitFor(() => expect(supportContextCalls()).toHaveLength(1));

    // Collapsing and reopening reuses the resolved links.
    await user.click(supportToggle);
    await user.click(supportToggle);
    await screen.findByRole('link', { name: /bug found/i });
    expect(supportContextCalls()).toHaveLength(1);
  });

  it('keeps filing disabled until the links resolve, and explains why it needs the network', async () => {
    let resolveContext: (value: unknown) => void = () => {};
    stubFetch(() => new Promise((resolve) => (resolveContext = resolve)));
    renderHome();

    await openSupportGroup();

    // While the context is in flight there is no typed entry to offer, so the
    // untyped one stands in -- disabled, and saying why.
    const issueItem = await screen.findByRole('link', { name: /file an issue/i });
    expect(issueItem).toHaveAttribute('aria-disabled', 'true');
    expect(issueItem).toHaveAttribute('href', '#');
    expect(issueItem).toHaveAttribute(
      'title',
      'Filing an issue opens GitHub and needs internet access and a GitHub account.'
    );

    resolveContext({
      ok: true,
      status: 200,
      json: () => Promise.resolve(CONTEXT_BODY),
    });

    // It is then replaced by the typed entries, each opening in a new tab.
    const bugFound = await screen.findByRole('link', { name: /bug found/i });
    expect(bugFound).toHaveAttribute('href', TICKET_TYPES[0].url);
    expect(bugFound).toHaveAttribute('title', expect.stringContaining(NETWORK_NOTE));
    expect(bugFound).toHaveAttribute('target', '_blank');
    expect(bugFound).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.queryByRole('link', { name: /file an issue/i })).not.toBeInTheDocument();
  });

  it('falls back to the untyped filing link when the backend reports no ticket types', async () => {
    // Graceful degradation, not a dead end: an install talking to an API that
    // predates #3811 still gets a working way to file, and answers the type
    // question on GitHub instead.
    stubFetch(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ issue_form_url: ISSUE_URL, network_note: NETWORK_NOTE }),
      })
    );
    renderHome();

    await openSupportGroup();

    const issueItem = await screen.findByRole('link', { name: /file an issue/i });
    await waitFor(() => expect(issueItem).toHaveAttribute('href', ISSUE_URL));
    expect(issueItem).toHaveAttribute('aria-disabled', 'false');
    expect(screen.queryByRole('link', { name: /bug found/i })).not.toBeInTheDocument();
  });

  it('leaves the item disabled rather than opening a dead link when the context request fails', async () => {
    stubFetch(() => Promise.reject(new Error('offline')));
    renderHome();

    await openSupportGroup();

    const issueItem = await screen.findByRole('link', { name: /file an issue/i });
    await waitFor(() => expect(supportContextCalls()).toHaveLength(1));
    expect(issueItem).toHaveAttribute('aria-disabled', 'true');
    expect(issueItem).toHaveAttribute('href', '#');
    // Docs are unaffected: reading never depended on the network.
    expect(screen.getByRole('link', { name: /docs/i })).toHaveAttribute('href', '/support/docs');
  });

  it('leaves the item disabled when the context endpoint answers with an error status', async () => {
    stubFetch(() =>
      Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) })
    );
    renderHome();

    await openSupportGroup();

    const issueItem = await screen.findByRole('link', { name: /file an issue/i });
    await waitFor(() => expect(supportContextCalls()).toHaveLength(1));
    expect(issueItem).toHaveAttribute('href', '#');
    expect(issueItem).toHaveAttribute('aria-disabled', 'true');
  });

  it('highlights each Support item on hover and clears it on leave', async () => {
    renderHome();

    await openSupportGroup();
    const docs = screen.getByRole('link', { name: /docs/i });
    const issue = await screen.findByRole('link', { name: /bug found/i });

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
    renderHome();

    const { user } = await openSupportGroup();
    const issueItem = await screen.findByRole('link', { name: /bug found/i });
    await waitFor(() => expect(issueItem).toHaveAttribute('href', TICKET_TYPES[0].url));

    // The href is a real off-box URL; stop the test environment from
    // following it while still letting React's own click handler run.
    const stopNavigation = (event: Event) => event.preventDefault();
    document.addEventListener('click', stopNavigation);
    try {
      await user.click(issueItem);
    } finally {
      document.removeEventListener('click', stopNavigation);
    }

    await waitFor(() => {
      expect(screen.queryByText('Theme')).not.toBeInTheDocument();
    });
  });

  it('drops a context response that arrives after the group is closed', async () => {
    let resolveContext: (value: unknown) => void = () => {};
    stubFetch(() => new Promise((resolve) => (resolveContext = resolve)));
    renderHome();

    const { user, supportToggle } = await openSupportGroup();
    await waitFor(() => expect(supportContextCalls()).toHaveLength(1));

    // Close the group while the request is still in flight, then let it land.
    await user.click(supportToggle);
    resolveContext({
      ok: true,
      status: 200,
      json: () => Promise.resolve(CONTEXT_BODY),
    });

    // Reopening re-requests, because the cancelled response was never stored.
    await user.click(supportToggle);
    await waitFor(() => expect(supportContextCalls()).toHaveLength(2));
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
