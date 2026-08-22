/**
 * Tests for Support -> File an Issue, the nyxGPT intake page (#3811).
 *
 * This is the page the owner's acceptance criterion describes: chosen from
 * Settings -> Support -> File an Issue, rendered by nyxGPT's own web
 * interface, asking every question the ticket needs, and followed by a
 * thank-you screen carrying the ticket number and a link to it. The filer
 * does not see github.com at any point on that path.
 *
 * What failed acceptance twice, and is therefore asserted against here:
 *
 * * the ticket type was asked in the *menu* and then asked again by whatever
 *   opened, so the first answer was pure friction (and, on the GitHub form,
 *   arrived as `None`);
 * * every degraded path -- no credential, a context call that failed --
 *   navigated the filer to the GitHub compose page rather than telling them
 *   anything.
 */
import { describe, it, expect } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import SupportNewTicketPage from '../../../src/app/support/new/page';

const ISSUE_FORM_URL = 'https://github.com/dkblinux98/nyxGPT/issues/new?template=support.yml';

const CONTEXT = {
  environment: { version: '3.0.0', platform: 'Linux 6.1 (x86_64)', python: '3.12.1' },
  ticket_types: [
    { value: 'Bug Found', description: 'Something is broken or behaving wrongly' },
    { value: 'Feature Request', description: 'Something nyxGPT should be able to do' },
    { value: 'Question', description: 'How do I …? / Is this supposed to …?' },
  ],
  can_submit: true,
  submit_route: '/api/v1/support/tickets',
  issue_form_url: ISSUE_FORM_URL,
};

const FILED = {
  status: 'filed',
  number: 4300,
  url: 'https://github.com/dkblinux98/nyxGPT/issues/4300',
  title: 'support: Add a dark mode toggle',
  labeled: true,
};

function serveContext(payload: unknown, status = 200) {
  server.use(
    http.get('/api/v1/support/context', () => HttpResponse.json(payload as object, { status }))
  );
}

/** Records what the page POSTs, and answers with `response`. */
function serveTickets(response: unknown, status = 201) {
  const seen: { body?: Record<string, unknown>; url?: string } = {};
  server.use(
    http.post('/api/v1/support/tickets', async ({ request }) => {
      seen.body = (await request.json()) as Record<string, unknown>;
      seen.url = request.url;
      return HttpResponse.json(response as object, { status });
    })
  );
  return seen;
}

async function fillAndSubmit(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText(/one-line summary/i), 'Add a dark mode toggle');
  await user.type(screen.getByLabelText(/what happened/i), 'It is too bright at night.');
  await user.click(screen.getByRole('button', { name: /submit/i }));
}

describe('Support intake page', () => {
  it('asks every question the ticket needs, on this page, and files it', async () => {
    serveContext(CONTEXT);
    const posted = serveTickets(FILED);
    const user = userEvent.setup();
    render(<SupportNewTicketPage />);

    // The type is asked HERE -- once. It was previously chosen in the menu
    // and then asked again by whatever the menu opened.
    const typeField = await screen.findByLabelText(/ticket type/i);
    expect(Array.from((typeField as HTMLSelectElement).options).map((o) => o.value)).toEqual([
      'Bug Found',
      'Feature Request',
      'Question',
    ]);
    await user.selectOptions(typeField, 'Feature Request');

    // Shown, not asked: the running install knows both.
    await screen.findByText(/nyxGPT 3\.0\.0 on Linux 6\.1/);

    await fillAndSubmit(user);

    await waitFor(() => expect(posted.body).toBeDefined());
    expect(posted.body).toEqual({
      ticket_type: 'Feature Request',
      summary: 'Add a dark mode toggle',
      description: 'It is too bright at night.',
    });
    // It posted where the backend said to, not to a route written twice.
    expect(posted.url).toContain('/api/v1/support/tickets');
  });

  it('shows the filed ticket with a link to it, and never a GitHub form', async () => {
    serveContext(CONTEXT);
    serveTickets(FILED);
    const user = userEvent.setup();
    render(<SupportNewTicketPage />);

    await user.selectOptions(await screen.findByLabelText(/ticket type/i), 'Feature Request');
    await fillAndSubmit(user);

    expect(await screen.findByText(/your ticket is filed/i)).toBeInTheDocument();
    const link = await screen.findByRole('link', { name: /ticket #4300/i });
    expect(link).toHaveAttribute('href', FILED.url);
    // A summary of what was filed, so the filer can see it without opening
    // the ticket: the type they chose and the title that was created.
    expect(screen.getByText('Feature Request')).toBeInTheDocument();
    expect(screen.getAllByText(FILED.title).length).toBeGreaterThan(0);
    // The way back is into the app, not out of it.
    expect(screen.getAllByRole('link', { name: /back to chat/i })[0]).toHaveAttribute('href', '/');
    expect(screen.queryByRole('link', { name: /report it on github/i })).not.toBeInTheDocument();
  });

  it('lets the filer start a second ticket without leaving the page', async () => {
    serveContext(CONTEXT);
    serveTickets(FILED);
    const user = userEvent.setup();
    render(<SupportNewTicketPage />);

    await screen.findByLabelText(/ticket type/i);
    await fillAndSubmit(user);
    await screen.findByText(/your ticket is filed/i);

    await user.click(screen.getByRole('button', { name: /file another/i }));

    // A blank form, not the previous ticket's text sitting in it.
    expect(await screen.findByLabelText(/one-line summary/i)).toHaveValue('');
    expect(screen.getByLabelText(/what happened/i)).toHaveValue('');
  });

  it('will not submit until both written answers are given', async () => {
    serveContext(CONTEXT);
    const user = userEvent.setup();
    render(<SupportNewTicketPage />);

    await screen.findByLabelText(/ticket type/i);
    const submit = screen.getByRole('button', { name: /submit/i });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText(/one-line summary/i), 'Something happened');
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText(/what happened/i), 'Here is what.');
    expect(submit).toBeEnabled();
  });

  it('explains a tokenless install on the page instead of sending the filer to GitHub', async () => {
    // The one case the product genuinely cannot file for. The previous
    // version navigated to github.com for it -- from the menu, before the
    // filer had typed anything. Now the page says what is wrong, and the
    // GitHub form is a link they may choose.
    serveContext({ ...CONTEXT, can_submit: false });
    render(<SupportNewTicketPage />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/cannot file tickets for you/i);
    expect(screen.getByText(/\[github\] pat/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/one-line summary/i)).not.toBeInTheDocument();
    const offer = screen.getByRole('link', { name: /report it on github/i });
    expect(offer).toHaveAttribute('href', ISSUE_FORM_URL);
    expect(offer).toHaveAttribute('target', '_blank');
    expect(offer).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('switches to the tokenless explanation when the backend answers 503 on submit', async () => {
    // `can_submit` said yes and filing said no: the credential was removed,
    // or the context came from a different config. The page must not offer a
    // pointless retry.
    serveContext(CONTEXT);
    serveTickets(
      {
        status: 'no_credential',
        detail: 'This nyxGPT install has no GitHub token configured.',
        issue_form_url: ISSUE_FORM_URL,
      },
      503
    );
    const user = userEvent.setup();
    render(<SupportNewTicketPage />);

    await screen.findByLabelText(/ticket type/i);
    await fillAndSubmit(user);

    expect(await screen.findByRole('alert')).toHaveTextContent(/cannot file tickets for you/i);
    expect(screen.getByRole('link', { name: /report it on github/i })).toHaveAttribute(
      'href',
      ISSUE_FORM_URL
    );
  });

  it('reports a refusal from GitHub in the filer’s words, keeping what they wrote', async () => {
    serveContext(CONTEXT);
    serveTickets(
      { error: { code: 'bad_gateway', message: 'GitHub refused the request.' } },
      502
    );
    const user = userEvent.setup();
    render(<SupportNewTicketPage />);

    await screen.findByLabelText(/ticket type/i);
    await fillAndSubmit(user);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/GitHub refused the request/);
    // Not "[object Object]" (#3831), and the ticket they typed is still there.
    expect(alert).not.toHaveTextContent('[object Object]');
    expect(screen.getByLabelText(/one-line summary/i)).toHaveValue('Add a dark mode toggle');
    // A way out that does not depend on this install, offered only now that
    // filing from here has actually failed.
    expect(screen.getByRole('link', { name: /report it on github instead/i })).toHaveAttribute(
      'href',
      ISSUE_FORM_URL
    );
  });

  it('reports an unreachable API rather than hanging on the spinner', async () => {
    serveContext(CONTEXT);
    server.use(http.post('/api/v1/support/tickets', () => HttpResponse.error()));
    const user = userEvent.setup();
    render(<SupportNewTicketPage />);

    await screen.findByLabelText(/ticket type/i);
    await fillAndSubmit(user);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /submit/i })).toBeEnabled();
  });

  it('still files when the context call fails, instead of leaving a dead page', async () => {
    // The lesson from the acceptance failure: this page must not decide what
    // it can do from a probe that might not answer. An unreachable context
    // endpoint costs the filer the version line -- not the ability to report
    // the very outage that broke it.
    server.use(http.get('/api/v1/support/context', () => HttpResponse.error()));
    const posted = serveTickets(FILED);
    const user = userEvent.setup();
    render(<SupportNewTicketPage />);

    const typeField = await screen.findByLabelText(/ticket type/i);
    expect(Array.from((typeField as HTMLSelectElement).options).map((o) => o.value)).toEqual([
      'Bug Found',
      'Feature Request',
      'Question',
    ]);
    await screen.findByText(/unknown version/i);

    await fillAndSubmit(user);

    await waitFor(() => expect(posted.body).toBeDefined());
    expect(posted.body).toMatchObject({ ticket_type: 'Bug Found' });
    expect(await screen.findByRole('link', { name: /ticket #4300/i })).toBeInTheDocument();
  });

  it('files from a backend that reports neither ticket types nor a submit route', async () => {
    // An API older than this page. It still answers the POST, so the page
    // uses the route this version serves rather than refusing to try.
    serveContext({ environment: { version: '2.1.0', platform: 'Darwin 24.5.0 (arm64)' } });
    const posted = serveTickets(FILED);
    const user = userEvent.setup();
    render(<SupportNewTicketPage />);

    await screen.findByLabelText(/ticket type/i);
    await screen.findByText(/nyxGPT 2\.1\.0 on Darwin 24\.5\.0/);
    await fillAndSubmit(user);

    await waitFor(() => expect(posted.body).toBeDefined());
    expect(posted.url).toContain('/api/v1/support/tickets');
  });

  it('files with a 503 that carries no fallback link, without inventing one', async () => {
    // The backend always sends `issue_form_url` with its 503 today. An older
    // one need not, and the page must still say what is wrong rather than
    // rendering a link to nowhere.
    serveContext({ ...CONTEXT, issue_form_url: undefined });
    serveTickets({ status: 'no_credential', detail: 'No token configured.' }, 503);
    const user = userEvent.setup();
    render(<SupportNewTicketPage />);

    await screen.findByLabelText(/ticket type/i);
    await fillAndSubmit(user);

    expect(await screen.findByRole('alert')).toHaveTextContent(/cannot file tickets for you/i);
    expect(screen.queryByRole('link', { name: /report it on github/i })).not.toBeInTheDocument();
  });

  it('labels a ticket type that arrives without a description', async () => {
    serveContext({ ...CONTEXT, ticket_types: [{ value: 'Bug Found', description: '' }] });
    render(<SupportNewTicketPage />);

    const typeField = (await screen.findByLabelText(/ticket type/i)) as HTMLSelectElement;
    await waitFor(() => expect(typeField.options).toHaveLength(1));
    expect(typeField.options[0].textContent).toBe('Bug Found');
  });

  it('drops a context response that lands after the page is closed', async () => {
    // The page can be left before its one fetch answers -- filing an issue is
    // exactly the moment someone is closing things in frustration. Writing
    // state into an unmounted component is a React warning today and a leak
    // in the making, so the effect's guard is exercised here rather than
    // trusted.
    let release: () => void = () => {};
    const inFlight = new Promise<void>((resolve) => {
      release = resolve;
    });
    let answered = false;
    server.use(
      http.get('/api/v1/support/context', async () => {
        await inFlight;
        answered = true;
        return HttpResponse.json(CONTEXT);
      })
    );
    const { unmount } = render(<SupportNewTicketPage />);

    // The form is usable while the context is still in flight.
    await screen.findByLabelText(/ticket type/i);
    unmount();
    release();

    // Wait for the response to actually land, so the effect's guard runs
    // rather than the test finishing before it is reached.
    await waitFor(() => expect(answered).toBe(true));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByLabelText(/ticket type/i)).not.toBeInTheDocument();
  });

  it('offers the fallback the 503 carries even when the context never answered', async () => {
    // Both halves failed: no context to read a link from, and no credential
    // to file with. The link in the refusal is then the only one there is,
    // and it has to survive being merged into an empty context.
    server.use(http.get('/api/v1/support/context', () => HttpResponse.error()));
    serveTickets(
      { status: 'no_credential', detail: 'No token.', issue_form_url: ISSUE_FORM_URL },
      503
    );
    const user = userEvent.setup();
    render(<SupportNewTicketPage />);

    await screen.findByLabelText(/ticket type/i);
    await fillAndSubmit(user);

    expect(await screen.findByRole('alert')).toHaveTextContent(/cannot file tickets for you/i);
    expect(screen.getByRole('link', { name: /report it on github/i })).toHaveAttribute(
      'href',
      ISSUE_FORM_URL
    );
  });

  it('still files when the context endpoint answers with an error status', async () => {
    // Distinct from an unreachable API: this one answered, and answered
    // uselessly. Either way the page keeps its own defaults rather than
    // treating a broken probe as a reason to stop taking reports.
    serveContext({ detail: 'boom' }, 500);
    const posted = serveTickets(FILED);
    const user = userEvent.setup();
    render(<SupportNewTicketPage />);

    await screen.findByLabelText(/ticket type/i);
    await screen.findByText(/unknown version/i);
    await fillAndSubmit(user);

    await waitFor(() => expect(posted.body).toBeDefined());
    expect(await screen.findByRole('link', { name: /ticket #4300/i })).toBeInTheDocument();
  });

  it('has a way back to the chat from the form itself', async () => {
    serveContext(CONTEXT);
    render(<SupportNewTicketPage />);

    await screen.findByLabelText(/ticket type/i);
    expect(screen.getByRole('link', { name: /cancel/i })).toHaveAttribute('href', '/');
    expect(screen.getByRole('link', { name: /back to chat/i })).toHaveAttribute('href', '/');
  });
});
