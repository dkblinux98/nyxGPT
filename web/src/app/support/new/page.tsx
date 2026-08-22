'use client';

// Support -> File an Issue: the intake page (#3811).
//
// This is a nyxGPT page, served by this Next.js app, the same way Support ->
// Docs is. It is deliberately a *route* and not a modal in the chat: the
// owner's acceptance criterion is "an nyxGPT webUI page renders when a user
// chooses Settings -> Support -> File an issue", followed by a thank-you
// screen with a link to the ticket. Both screens live here.
//
// Two shapes this replaced, and why neither survived acceptance:
//
//   * github.com's compose page (#3881). It shows a person with a broken
//     install this *development* repository's sidebar -- assignees, dev
//     labels, dev projects, milestones -- and leaves them there afterwards.
//   * a menu that asked the ticket type first and then decided, from a
//     runtime probe (`can_submit`), whether to open an in-chat dialog or
//     hand the filer to GitHub anyway (#3964). Every degraded path of that
//     probe ended on github.com, which is the one destination the spec
//     rules out, and the type question was asked twice -- once in the menu,
//     again on the form that opened.
//
// So the menu entry is a plain link to this page and knows nothing else. The
// type is asked here, once. GitHub is reached only by an install that holds
// no credential at all, and then only as a link this page offers and the
// filer chooses to click -- never as somewhere they are sent.

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { apiErrorText, errorMessage } from '../../../lib/apiError';

/** One entry of `support_context().ticket_types`. */
interface TicketTypeOption {
  value: string;
  description: string;
}

interface SupportContext {
  environment?: { version?: string; platform?: string; python?: string };
  ticket_types?: TicketTypeOption[];
  /** Whether this install holds a GitHub credential and can file at all. */
  can_submit?: boolean;
  /** Where to POST. Reported by the backend so the two cannot drift apart. */
  submit_route?: string;
  /** GitHub's prefilled form -- the tokenless install's only route out. */
  issue_form_url?: string;
}

/** What the backend reports for a ticket it created. */
interface FiledTicket {
  number: number;
  url: string;
  title: string;
  /**
   * False means GitHub dropped the `Support` label (a token without push
   * access). The ticket exists and `support_intake_guard.yml` repairs the
   * label, so this is not shown to the filer as a failure.
   */
  labeled?: boolean;
}

// The taxonomy the backend reports. Duplicated here ONLY as the fallback for
// a context call that did not answer: the page must still be able to file a
// ticket when the one non-essential fetch on it fails, because a support
// form that breaks when something is broken is no support form. The backend
// validates the type regardless (`support._validated_ticket`), so a drift
// between these lists surfaces as a 400 naming the type, not a bad ticket.
const FALLBACK_TICKET_TYPES: TicketTypeOption[] = [
  { value: 'Bug Found', description: 'Something is broken or behaving wrongly' },
  { value: 'Feature Request', description: 'Something nyxGPT should be able to do' },
  { value: 'Question', description: 'How do I ...?' },
];

const DEFAULT_SUBMIT_ROUTE = '/api/v1/support/tickets';

const FIELD_STYLE: React.CSSProperties = {
  width: '100%',
  padding: '10px 12px',
  borderRadius: 6,
  border: '1px solid var(--border-light)',
  background: 'var(--background)',
  color: 'var(--foreground)',
  fontSize: 14,
  fontFamily: 'inherit',
};

const LABEL_STYLE: React.CSSProperties = {
  display: 'block',
  fontSize: 13,
  fontWeight: 600,
  marginBottom: 6,
};

const CARD_STYLE: React.CSSProperties = {
  border: '1px solid var(--border-light)',
  borderRadius: 8,
  padding: 20,
  background: 'var(--background)',
};

export default function SupportNewTicketPage() {
  const [context, setContext] = useState<SupportContext | null>(null);
  const [loading, setLoading] = useState(true);
  const [ticketType, setTicketType] = useState(FALLBACK_TICKET_TYPES[0].value);
  const [summary, setSummary] = useState('');
  const [description, setDescription] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** Set when the backend answered 503: this install cannot file for anyone. */
  const [noCredential, setNoCredential] = useState(false);
  const [filed, setFiled] = useState<FiledTicket | null>(null);
  /** What was filed, kept for the thank-you screen's summary of the ticket. */
  const [filedType, setFiledType] = useState('');

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch('/api/v1/support/context');
        if (res.ok) {
          const data: SupportContext = await res.json();
          if (!cancelled) setContext(data);
        }
      } catch {
        // The form still works without it -- see FALLBACK_TICKET_TYPES.
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const ticketTypes =
    context?.ticket_types && context.ticket_types.length > 0
      ? context.ticket_types
      : FALLBACK_TICKET_TYPES;
  const environment = context?.environment;
  const fallbackUrl = context?.issue_form_url;
  // `can_submit === false` is a definite answer from a backend that read the
  // configuration; `undefined` (older backend, or a context call that failed)
  // is not, and must not stop anyone filing. Only the definite no is acted on.
  const cannotFile = context?.can_submit === false || noCredential;

  const submit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setSubmitting(true);
      setError(null);
      try {
        const res = await fetch(context?.submit_route ?? DEFAULT_SUBMIT_ROUTE, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ticket_type: ticketType,
            summary: summary.trim(),
            description: description.trim(),
          }),
        });
        const data = await res.json();
        if (!res.ok) {
          // 503 is not "try again": this install holds no credential, so the
          // page switches to saying so rather than showing a retryable error.
          if (res.status === 503) {
            setNoCredential(true);
            if (data?.issue_form_url) {
              setContext((prev) => ({ ...(prev ?? {}), issue_form_url: data.issue_form_url }));
            }
            return;
          }
          // Never interpolate `data.detail` directly: the API's envelope and
          // FastAPI's own refusals are different shapes and either renders as
          // [object Object] (#3831).
          throw new Error(apiErrorText(data, `HTTP ${res.status}`));
        }
        setFiledType(ticketType);
        setFiled({
          number: data.number,
          url: data.url,
          title: data.title,
          labeled: data.labeled,
        });
      } catch (err) {
        setError(errorMessage(err));
      } finally {
        setSubmitting(false);
      }
    },
    [context?.submit_route, description, summary, ticketType]
  );

  const canSubmit = summary.trim().length > 0 && description.trim().length > 0 && !submitting;

  return (
    <div
      style={{
        minHeight: '100vh',
        background: 'var(--background)',
        color: 'var(--foreground)',
        padding: 24,
      }}
    >
      <div style={{ maxWidth: 720, margin: '0 auto' }}>
        <div style={{ marginBottom: 24 }}>
          <h1 style={{ fontSize: 28, fontWeight: 600, margin: 0, marginBottom: 8 }}>
            {filed ? 'Ticket filed' : 'File an Issue'}
          </h1>
          <Link href="/" style={{ color: '#0066cc', textDecoration: 'none' }}>
            ← Back to chat
          </Link>
        </div>

        {filed ? (
          <div style={CARD_STYLE}>
            <h2 style={{ margin: '0 0 8px', fontSize: 20 }}>
              🎉 Thanks — your ticket is filed!
            </h2>
            <p style={{ margin: '0 0 16px', fontSize: 14, lineHeight: 1.6 }}>
              nyxGPT sent it straight to the support queue, so you never had to leave the app.
              Someone will pick it up from there — you can follow along, or add anything you
              forgot, on the ticket itself.
            </p>

            {/* The summary of what was filed: the filer should be able to see
                what they just sent without opening GitHub to find out. */}
            <dl
              style={{
                margin: '0 0 16px',
                display: 'grid',
                gridTemplateColumns: 'auto 1fr',
                gap: '6px 12px',
                fontSize: 14,
              }}
            >
              <dt style={{ opacity: 0.7 }}>Type</dt>
              <dd style={{ margin: 0 }}>{filedType}</dd>
              <dt style={{ opacity: 0.7 }}>Summary</dt>
              <dd style={{ margin: 0 }}>{filed.title}</dd>
              <dt style={{ opacity: 0.7 }}>Sent with it</dt>
              <dd style={{ margin: 0 }}>
                nyxGPT {environment?.version ?? 'unknown version'} on{' '}
                {environment?.platform ?? 'an unknown platform'}
              </dd>
            </dl>

            <p style={{ margin: '0 0 20px', fontSize: 16 }}>
              <a
                href={filed.url}
                target="_blank"
                rel="noopener noreferrer"
                style={{ color: '#0066cc', fontWeight: 600, textDecoration: 'none' }}
              >
                🎟️ Ticket #{filed.number}: {filed.title}
              </a>
            </p>

            <div style={{ display: 'flex', gap: 8 }}>
              <Link href="/" style={primaryLinkStyle()}>
                Back to chat
              </Link>
              <button
                type="button"
                onClick={() => {
                  setFiled(null);
                  setSummary('');
                  setDescription('');
                }}
                style={{
                  padding: '8px 14px',
                  borderRadius: 6,
                  border: '1px solid var(--border-light)',
                  background: 'transparent',
                  color: 'var(--foreground)',
                  fontSize: 14,
                  cursor: 'pointer',
                }}
              >
                File another
              </button>
            </div>
          </div>
        ) : cannotFile ? (
          // The one case the product genuinely cannot cover: no `[github] pat`
          // means nothing to file with. The page says so and offers GitHub's
          // form as a link the filer may choose -- it does not send them there.
          <div style={CARD_STYLE} role="alert">
            <h2 style={{ margin: '0 0 8px', fontSize: 18 }}>
              This install cannot file tickets for you
            </h2>
            <p style={{ margin: '0 0 12px', fontSize: 14, lineHeight: 1.6 }}>
              nyxGPT files support tickets using the GitHub token in its own configuration, and
              this install has none. Add one with <code>nyxgpt config</code> (the{' '}
              <code>[github] pat</code> setting) and this page will file for you.
            </p>
            {fallbackUrl && (
              <p style={{ margin: 0, fontSize: 14, lineHeight: 1.6 }}>
                In the meantime you can{' '}
                <a
                  href={fallbackUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ color: '#0066cc' }}
                >
                  report it on GitHub
                </a>{' '}
                — the form opens with your version and platform already filled in, and you pick
                the ticket type there.
              </p>
            )}
          </div>
        ) : (
          <form onSubmit={submit} style={CARD_STYLE}>
            <p style={{ margin: '0 0 20px', fontSize: 14, opacity: 0.8, lineHeight: 1.6 }}>
              Tell us what happened in your own words — you do not need to know anything about
              nyxGPT&apos;s internals. nyxGPT files this for you and shows you the ticket when
              it is done.
            </p>

            <div style={{ marginBottom: 16 }}>
              <label htmlFor="support-ticket-type" style={LABEL_STYLE}>
                Ticket type
              </label>
              <select
                id="support-ticket-type"
                value={ticketType}
                onChange={(event) => setTicketType(event.target.value)}
                style={FIELD_STYLE}
              >
                {ticketTypes.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.description
                      ? `${option.value} — ${option.description}`
                      : option.value}
                  </option>
                ))}
              </select>
            </div>

            <div style={{ marginBottom: 16 }}>
              <label htmlFor="support-ticket-summary" style={LABEL_STYLE}>
                One-line summary
              </label>
              <input
                id="support-ticket-summary"
                value={summary}
                maxLength={120}
                onChange={(event) => setSummary(event.target.value)}
                placeholder="Sending a message spins forever"
                style={FIELD_STYLE}
              />
            </div>

            <div style={{ marginBottom: 16 }}>
              <label htmlFor="support-ticket-description" style={LABEL_STYLE}>
                What happened?
              </label>
              <textarea
                id="support-ticket-description"
                value={description}
                maxLength={8000}
                rows={8}
                onChange={(event) => setDescription(event.target.value)}
                placeholder={
                  'What were you trying to do, what did you expect, and what happened ' +
                  'instead? Paste any error message you saw.'
                }
                style={{ ...FIELD_STYLE, resize: 'vertical' }}
              />
            </div>

            {/* Shown, not asked: the running install knows both, and the filer
                should be able to see exactly what gets attached to the ticket. */}
            <p style={{ margin: '0 0 16px', fontSize: 12, opacity: 0.7 }}>
              {loading
                ? 'Reading this install’s version and platform…'
                : `Sent with your report: nyxGPT ${environment?.version ?? 'unknown version'} on ${
                    environment?.platform ?? 'an unknown platform'
                  }.`}
            </p>

            {error && (
              <div
                role="alert"
                style={{
                  marginBottom: 16,
                  padding: '10px 12px',
                  borderRadius: 6,
                  border: '1px solid var(--error-border, #f0a0a0)',
                  fontSize: 13,
                  lineHeight: 1.5,
                }}
              >
                <div>{error}</div>
                {/* Offered only after filing from here failed: a path that
                    does not depend on this install beats "give up". */}
                {fallbackUrl && (
                  <a
                    href={fallbackUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{ display: 'inline-block', marginTop: 8, color: '#0066cc' }}
                  >
                    Report it on GitHub instead
                  </a>
                )}
              </div>
            )}

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <Link
                href="/"
                style={{
                  padding: '8px 14px',
                  borderRadius: 6,
                  border: '1px solid var(--border-light)',
                  color: 'var(--foreground)',
                  fontSize: 14,
                  textDecoration: 'none',
                }}
              >
                Cancel
              </Link>
              <button
                type="submit"
                disabled={!canSubmit}
                style={{
                  padding: '8px 14px',
                  borderRadius: 6,
                  border: 'none',
                  background: 'var(--accent, #3b82f6)',
                  color: '#fff',
                  fontSize: 14,
                  fontWeight: 600,
                  cursor: canSubmit ? 'pointer' : 'not-allowed',
                  opacity: canSubmit ? 1 : 0.6,
                }}
              >
                {submitting ? 'Filing…' : 'Submit'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

/** The filled primary action, as a link (the thank-you screen's "Back to chat"). */
function primaryLinkStyle(): React.CSSProperties {
  return {
    padding: '8px 14px',
    borderRadius: 6,
    background: 'var(--accent, #3b82f6)',
    color: '#fff',
    fontSize: 14,
    fontWeight: 600,
    textDecoration: 'none',
  };
}
