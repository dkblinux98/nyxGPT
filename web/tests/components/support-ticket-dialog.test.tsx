/**
 * Tests for the in-app support intake dialog (#3811).
 *
 * The acceptance failure this covers is about where the user *is*: filing a
 * ticket used to send them to github.com's compose page and leave them
 * there. So these pin the whole round trip inside the app -- the questions,
 * the POST, and a success screen that shows the filer their own ticket -- as
 * well as the two ways it can go wrong (the backend refuses; the request
 * never lands), where the GitHub form reappears as an explicit fallback
 * rather than as the default surface.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SupportTicketDialog from '@/components/SupportTicketDialog';

const TICKET_TYPES = [
  { value: 'Bug Found', description: 'Something is broken or behaving wrongly' },
  { value: 'Feature Request', description: 'Something nyxGPT should be able to do' },
  { value: 'Question', description: 'How do I …?' },
];

const ENVIRONMENT = { version: '3.0.0', platform: 'Linux 6.8.0 (x86_64)', python: '3.12.1' };

const FILED = {
  status: 'filed',
  number: 4300,
  url: 'https://github.com/dkblinux98/nyxGPT/issues/4300',
  title: 'support: Docs are a mess',
  labeled: true,
};

function renderDialog(overrides: Partial<React.ComponentProps<typeof SupportTicketDialog>> = {}) {
  const onClose = vi.fn();
  const rendered = render(
    <SupportTicketDialog
      initialType="Bug Found"
      ticketTypes={TICKET_TYPES}
      submitRoute="/api/v1/support/tickets"
      environment={ENVIRONMENT}
      fallbackUrl="https://github.com/dkblinux98/nyxGPT/issues/new?template=support.yml"
      onClose={onClose}
      {...overrides}
    />
  );
  return { onClose, rendered, user: userEvent.setup() };
}

/** Fill the two required answers. */
async function fillTicket(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText(/one-line summary/i), 'Docs are a mess');
  await user.type(screen.getByLabelText(/what happened/i), 'I cannot find the install steps.');
}

function respondWith(status: number, data: unknown) {
  global.fetch = vi.fn().mockResolvedValue({
    ok: status < 400,
    status,
    json: () => Promise.resolve(data),
  });
}

describe('SupportTicketDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    respondWith(201, FILED);
  });

  it('opens on the type the filer picked in the menu, without asking again', async () => {
    renderDialog({ initialType: 'Question' });

    expect(screen.getByRole('dialog', { name: /file a support ticket/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/ticket type/i)).toHaveValue('Question');
  });

  it('shows the version and platform it will send rather than asking for them', async () => {
    // A user reporting a bug should not have to look up their own version --
    // the running install already knows it.
    renderDialog();

    expect(screen.getByText(/nyxGPT 3\.0\.0 on Linux 6\.8\.0 \(x86_64\)/)).toBeInTheDocument();
  });

  it('says so plainly when the install reported no environment', () => {
    // Degraded, not broken: an API that predates #3811 sends no environment,
    // and "unknown version" beats rendering the word "undefined" at someone
    // who is already having a bad day.
    renderDialog({ environment: null });

    expect(screen.getByText(/unknown version on an unknown platform/i)).toBeInTheDocument();
  });

  it('cannot be submitted until both questions are answered', async () => {
    const { user } = renderDialog();

    const submit = screen.getByRole('button', { name: /file ticket/i });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText(/one-line summary/i), 'Docs are a mess');
    expect(submit).toBeDisabled();

    // Whitespace is not an answer.
    await user.type(screen.getByLabelText(/what happened/i), '   ');
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText(/what happened/i), 'It spins forever.');
    expect(submit).toBeEnabled();
  });

  it('files the ticket from nyxGPT and shows the filer their own ticket', async () => {
    const { user } = renderDialog();
    await fillTicket(user);

    await user.click(screen.getByRole('button', { name: /file ticket/i }));

    const [url, options] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe('/api/v1/support/tickets');
    expect(options.method).toBe('POST');
    expect(JSON.parse(options.body)).toEqual({
      ticket_type: 'Bug Found',
      summary: 'Docs are a mess',
      description: 'I cannot find the install steps.',
    });

    // The success screen: a link to their ticket, and no mention of anything
    // about this repository.
    const link = await screen.findByRole('link', { name: /ticket #4300/i });
    expect(link).toHaveAttribute('href', FILED.url);
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByText(/your ticket is filed/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/one-line summary/i)).not.toBeInTheDocument();
  });

  it('sends the type the filer switched to, not the one the menu opened with', async () => {
    const { user } = renderDialog();
    await user.selectOptions(screen.getByLabelText(/ticket type/i), 'Feature Request');
    await fillTicket(user);

    await user.click(screen.getByRole('button', { name: /file ticket/i }));

    await screen.findByRole('link', { name: /ticket #4300/i });
    const [, options] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(options.body).ticket_type).toBe('Feature Request');
  });

  it('trims what it sends, so a stray newline is not part of the ticket', async () => {
    const { user } = renderDialog();
    await user.type(screen.getByLabelText(/one-line summary/i), '  spaced  ');
    await user.type(screen.getByLabelText(/what happened/i), '  it broke  ');

    await user.click(screen.getByRole('button', { name: /file ticket/i }));

    await screen.findByRole('link', { name: /ticket #4300/i });
    const [, options] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(options.body)).toMatchObject({ summary: 'spaced', description: 'it broke' });
  });

  it('shows a filing in progress rather than letting it be submitted twice', async () => {
    let land: (value: unknown) => void = () => {};
    global.fetch = vi.fn().mockImplementation(() => new Promise((resolve) => (land = resolve)));
    const { user } = renderDialog();
    await fillTicket(user);

    await user.click(screen.getByRole('button', { name: /file ticket/i }));

    const submitting = await screen.findByRole('button', { name: /filing/i });
    expect(submitting).toBeDisabled();

    land({ ok: true, status: 201, json: () => Promise.resolve(FILED) });
    await screen.findByRole('link', { name: /ticket #4300/i });
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it('keeps the answers and offers GitHub when the backend refuses', async () => {
    // The refusal that matters most: an install with no credential. The
    // honest answer is the prefilled form, not "you cannot report this" --
    // and the typing must survive, because retyping a bug report is how a
    // user gives up on filing it.
    respondWith(503, {
      status: 'no_credential',
      detail: 'This nyxGPT install has no GitHub token configured.',
      issue_form_url: 'https://github.com/dkblinux98/nyxGPT/issues/new?template=support.yml',
    });
    const { user } = renderDialog();
    await fillTicket(user);

    await user.click(screen.getByRole('button', { name: /file ticket/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/no GitHub token configured/i);
    expect(screen.getByRole('link', { name: /report it on github instead/i })).toHaveAttribute(
      'href',
      'https://github.com/dkblinux98/nyxGPT/issues/new?template=support.yml'
    );
    expect(screen.getByLabelText(/one-line summary/i)).toHaveValue('Docs are a mess');
    // Retrying is possible: the button is live again, not stuck on "Filing…".
    expect(screen.getByRole('button', { name: /file ticket/i })).toBeEnabled();
  });

  it('reports a backend error envelope as text, never as [object Object]', async () => {
    // #3831's shape: `detail` can be an object, and interpolating one hides
    // the actual failure behind [object Object].
    respondWith(502, { error: { code: 'bad_gateway', message: 'GitHub refused the request.' } });
    const { user } = renderDialog();
    await fillTicket(user);

    await user.click(screen.getByRole('button', { name: /file ticket/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('GitHub refused the request.');
    expect(alert).not.toHaveTextContent('[object Object]');
  });

  it('reports a request that never landed', async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error('Failed to fetch'));
    const { user } = renderDialog();
    await fillTicket(user);

    await user.click(screen.getByRole('button', { name: /file ticket/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/failed to fetch/i);
  });

  it('omits the GitHub fallback when there is no form URL to offer', async () => {
    // Nothing to point at is not a reason to render a dead link.
    respondWith(502, { detail: 'GitHub refused the request.' });
    const { user } = renderDialog({ fallbackUrl: null });
    await fillTicket(user);

    await user.click(screen.getByRole('button', { name: /file ticket/i }));

    await screen.findByRole('alert');
    expect(screen.queryByRole('link', { name: /report it on github/i })).not.toBeInTheDocument();
  });

  it('closes from Cancel, from the backdrop, and with Escape', async () => {
    const { onClose, user } = renderDialog();

    await user.click(screen.getByRole('button', { name: /cancel/i }));
    expect(onClose).toHaveBeenCalledTimes(1);

    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(2);

    // The backdrop dismisses; a click inside the panel must not.
    await user.click(screen.getByRole('dialog'));
    expect(onClose).toHaveBeenCalledTimes(2);
    await user.click(screen.getByRole('dialog').parentElement as HTMLElement);
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it('returns the filer to the chat from the success screen', async () => {
    const { onClose, user } = renderDialog();
    await fillTicket(user);
    await user.click(screen.getByRole('button', { name: /file ticket/i }));
    await screen.findByRole('link', { name: /ticket #4300/i });

    await user.click(screen.getByRole('button', { name: /back to chat/i }));

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('stops listening for Escape once it is closed', async () => {
    // The listener is on `document`, so leaving it attached would have a
    // dismissed dialog answering key presses meant for the chat behind it.
    const { onClose, rendered, user } = renderDialog();

    rendered.unmount();
    await user.keyboard('{Escape}');

    expect(onClose).not.toHaveBeenCalled();
  });

  it('does not fetch anything until the filer submits', async () => {
    const { user } = renderDialog();
    await fillTicket(user);
    await waitFor(() => expect(global.fetch).not.toHaveBeenCalled());
    await user.click(screen.getByRole('button', { name: /file ticket/i }));
    await screen.findByRole('link', { name: /ticket #4300/i });
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });
});
