/**
 * Tests for the chat page's Settings -> Support group (#3745, #3811).
 *
 * Docs and File an Issue behave differently on purpose: Docs is packaged with
 * the install and always works, while filing needs the network, so the
 * context is resolved lazily (only when the group is opened) and nothing
 * usable is offered until that resolves.
 *
 * Filing is one entry per ticket type, and what an entry *does* depends on
 * whether this install can file (`can_submit`, i.e. it has a GitHub
 * credential):
 *
 * * it can -- the entry opens the in-app form and nyxGPT files the ticket.
 *   This is the surface the owner accepted the issue against: the filer
 *   never sees github.com, which used to show a user with a broken install
 *   this repository's development metadata and then strand them there.
 * * it cannot -- the same entries degrade to GitHub's prefilled form, the
 *   one case the product genuinely cannot cover for the user.
 *
 * The silent-failure path is pinned either way: a broken context fetch must
 * leave the item disabled rather than open a dead GitHub URL.
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

const ISSUE_REPO_URL = 'https://github.com/dkblinux8/nyxGPT';
const ISSUE_URL = `${ISSUE_REPO_URL}/issues/new?template=support.yml`;
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

/** An install that holds a GitHub credential: nyxGPT files the ticket. */
const CONTEXT_BODY = {
  issue_form_url: ISSUE_URL,
  ticket_types: TICKET_TYPES,
  network_note: NETWORK_NOTE,
  can_submit: true,
  submit_route: '/api/v1/support/tickets',
  environment: { version: '3.0.0', platform: 'Linux 6.8.0 (x86_64)', python: '3.12.1' },
};

/** An install with no credential: the GitHub form is all it can offer. */
const TOKENLESS_CONTEXT_BODY = {
  issue_form_url: ISSUE_URL,
  ticket_types: TICKET_TYPES,
  network_note: NETWORK_NOTE,
  can_submit: false,
};

/**
 * Stubs every request the chat page makes, with `/api/v1/support/context`
 * answered however the test needs.
 */
function stubFetch(supportContext: () => Promise<unknown>) {
  global.fetch = vi.fn().mockImplementation((url: string) => {
    if (url.includes('/api/v1/support/context')) return supportContext();
    if (url.includes('/api/v1/support/tickets')) {
      // What the backend answers once it has filed the ticket (#3811).
      return Promise.resolve({
        ok: true,
        status: 201,
        json: () =>
          Promise.resolve({
            status: 'filed',
            number: 4300,
            url: `${ISSUE_REPO_URL}/issues/4300`,
            title: 'support: Add a dark mode toggle',
            labeled: true,
          }),
      });
    }
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
    expect(screen.queryByRole('button', { name: /bug found/i })).not.toBeInTheDocument();

    await user.click(supportToggle);
    expect(supportToggle).toHaveAttribute('aria-expanded', 'true');

    expect(screen.getByRole('link', { name: /docs/i })).toHaveAttribute('href', '/support/docs');

    // One filing entry per ticket type (#3811). On an install that can file,
    // they are buttons rather than links: the ticket is created from here,
    // so there is nowhere to navigate to. Docs remains the group's only
    // link, which is what pins that nothing here still points at GitHub.
    for (const ticketType of TICKET_TYPES) {
      // Regex, not an exact name: each entry renders its emoji alongside the
      // label, so the accessible name is "🐛 Bug Found".
      const entry = await screen.findByRole('button', {
        name: new RegExp(ticketType.value, 'i'),
      });
      expect(entry).toHaveAttribute('title', ticketType.description);
    }
    expect(screen.getByRole('group', { name: 'Support' }).querySelectorAll('a')).toHaveLength(1);
  });

  it('opens the in-app form on the type that was clicked, and files without leaving the app', async () => {
    // The acceptance failure, in one test: the filer stays in nyxGPT from
    // the menu entry all the way to a ticket number.
    renderHome();
    const { user } = await openSupportGroup();

    await user.click(await screen.findByRole('button', { name: /feature request/i }));

    const dialog = await screen.findByRole('dialog', { name: /file a support ticket/i });
    expect(dialog).toBeInTheDocument();
    // Preselected: the classification made in the menu is not asked again.
    expect(screen.getByLabelText(/ticket type/i)).toHaveValue('Feature Request');
    // The environment the install reported, shown rather than asked for.
    expect(screen.getByText(/nyxGPT 3\.0\.0 on Linux 6\.8\.0/)).toBeInTheDocument();
    // ...and the menu got out of the way.
    expect(screen.queryByText('Theme')).not.toBeInTheDocument();

    await user.type(screen.getByLabelText(/one-line summary/i), 'Add a dark mode toggle');
    await user.type(screen.getByLabelText(/what happened/i), 'It is too bright at night.');
    await user.click(screen.getByRole('button', { name: /file ticket/i }));

    const link = await screen.findByRole('link', { name: /ticket #4300/i });
    expect(link).toHaveAttribute('href', 'https://github.com/dkblinux8/nyxGPT/issues/4300');
    // It posted to the route the backend advertised, not one hardcoded here.
    const [postUrl] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls.find(([url]) =>
      String(url).includes('/api/v1/support/tickets')
    ) as [string, RequestInit];
    expect(postUrl).toBe('/api/v1/support/tickets');
  });

  it('falls back to the canonical intake route when the backend names none', async () => {
    // Version skew, not a hypothetical: `can_submit` and `submit_route` come
    // from the same response, so an API reporting one without the other is an
    // API mid-upgrade. Filing still has to work -- the route it defaults to
    // is the one this version of the backend serves.
    stubFetch(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ ...CONTEXT_BODY, submit_route: undefined }),
      })
    );
    renderHome();
    const { user } = await openSupportGroup();

    await user.click(await screen.findByRole('button', { name: /bug found/i }));
    await user.type(screen.getByLabelText(/one-line summary/i), 'It broke');
    await user.type(screen.getByLabelText(/what happened/i), 'Everything, at once.');
    await user.click(screen.getByRole('button', { name: /file ticket/i }));

    await screen.findByRole('link', { name: /ticket #4300/i });
    expect(global.fetch).toHaveBeenCalledWith(
      '/api/v1/support/tickets',
      expect.objectContaining({ method: 'POST' })
    );
  });

  it('closes the form and returns to the chat', async () => {
    renderHome();
    const { user } = await openSupportGroup();
    await user.click(await screen.findByRole('button', { name: /bug found/i }));
    await screen.findByRole('dialog');

    await user.click(screen.getByRole('button', { name: /cancel/i }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('degrades to the prefilled GitHub form on an install with no credential', async () => {
    // The one case the product cannot file for. Offering the form beats
    // telling a user their report cannot be made.
    stubFetch(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(TOKENLESS_CONTEXT_BODY),
      })
    );
    renderHome();

    const { user } = await openSupportGroup();

    for (const ticketType of TICKET_TYPES) {
      const entry = await screen.findByRole('link', { name: new RegExp(ticketType.value, 'i') });
      expect(entry).toHaveAttribute('href', ticketType.url);
      expect(entry).toHaveAttribute('target', '_blank');
      expect(entry).toHaveAttribute('rel', 'noopener noreferrer');
      expect(entry).toHaveAttribute('title', expect.stringContaining(NETWORK_NOTE));
    }
    expect(screen.queryByRole('button', { name: /bug found/i })).not.toBeInTheDocument();

    // The degraded entries are real menu items, not decorations: they have
    // to highlight and dismiss the menu exactly like the buttons do.
    // Asserting only hrefs would leave their handlers unexecuted, which is
    // how a path only tokenless installs take rots unnoticed.
    const bugFound = screen.getByRole('link', { name: /bug found/i });
    fireEvent.mouseEnter(bugFound);
    expect(bugFound.style.background).toBe('var(--button-hover)');
    // happy-dom refuses to overwrite a `var()` background shorthand with a
    // keyword -- same workaround as the hover test below.
    bugFound.style.background = '';
    fireEvent.mouseLeave(bugFound);
    expect(bugFound.style.background).toBe('transparent');

    // The href is a real off-box URL; stop the test environment from
    // following it while still letting React's own click handler run.
    const stopNavigation = (event: Event) => event.preventDefault();
    document.addEventListener('click', stopNavigation);
    try {
      await user.click(bugFound);
    } finally {
      document.removeEventListener('click', stopNavigation);
    }
    await waitFor(() => expect(screen.queryByText('Theme')).not.toBeInTheDocument());
    // Handing over is not the same as filing here: no form opened.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
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

    // Collapsing and reopening reuses the resolved context.
    await user.click(supportToggle);
    await user.click(supportToggle);
    await screen.findByRole('button', { name: /bug found/i });
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

    // It is then replaced by the typed entries -- buttons, because this
    // install can file the ticket itself.
    const bugFound = await screen.findByRole('button', { name: /bug found/i });
    expect(bugFound).toHaveAttribute('title', TICKET_TYPES[0].description);
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

    const { user } = await openSupportGroup();

    const issueItem = await screen.findByRole('link', { name: /file an issue/i });
    await waitFor(() => expect(issueItem).toHaveAttribute('href', ISSUE_URL));
    expect(issueItem).toHaveAttribute('aria-disabled', 'false');
    expect(screen.queryByRole('button', { name: /bug found/i })).not.toBeInTheDocument();

    // The fallback is a real menu item, not a decoration: it has to highlight
    // and dismiss the menu exactly like a typed entry. Asserting only its href
    // would leave its handlers unexecuted, which is how a degraded-install
    // path rots without anyone noticing.
    fireEvent.mouseEnter(issueItem);
    expect(issueItem.style.background).toBe('var(--button-hover)');
    // happy-dom refuses to overwrite a `var()` background shorthand with a
    // keyword -- same workaround as the hover test above.
    issueItem.style.background = '';
    fireEvent.mouseLeave(issueItem);
    expect(issueItem.style.background).toBe('transparent');

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

  it('titles a typed entry with its description alone when the backend sends no network note', async () => {
    // The `.trim()` on the title exists for exactly this: the note is the
    // second sentence, so an install whose API omits it must get
    // "Something is broken or behaving wrongly." and not a dangling space or
    // the word "undefined" hanging off the tooltip.
    stubFetch(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ issue_form_url: ISSUE_URL, ticket_types: TICKET_TYPES }),
      })
    );
    renderHome();

    await openSupportGroup();

    const bugFound = await screen.findByRole('link', { name: /bug found/i });
    expect(bugFound).toHaveAttribute('title', `${TICKET_TYPES[0].description}.`);
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
    const issue = await screen.findByRole('button', { name: /bug found/i });

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

  it('closes the Settings menu when a filing entry is clicked', async () => {
    // The menu has to get out of the way: the form it opens is mounted at
    // the page root, and a menu left standing over it would cover it.
    renderHome();

    const { user } = await openSupportGroup();
    await user.click(await screen.findByRole('button', { name: /bug found/i }));

    await waitFor(() => {
      expect(screen.queryByText('Theme')).not.toBeInTheDocument();
    });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
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
