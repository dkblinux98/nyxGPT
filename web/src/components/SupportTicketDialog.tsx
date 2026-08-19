'use client';

import { useCallback, useEffect, useState } from 'react';
import { apiErrorText, errorMessage } from '../lib/apiError';

/**
 * The in-app support intake (#3811).
 *
 * The acceptance failure this exists to fix: Support -> File an Issue used to
 * hand the user to `github.com/.../issues/new`, which shows a person with a
 * broken install the *development* repository's compose sidebar -- assignees,
 * dev labels, dev projects, milestones, a contributing-guidelines footer --
 * and then leaves them on GitHub afterwards. None of that is theirs.
 *
 * So the questions are asked here, in the chat, and nyxGPT files the ticket
 * itself (`POST /api/v1/support/tickets`). What the filer sees at the end is
 * their own ticket number and a link to it; what they do NOT see is anything
 * about this repository's internals.
 *
 * Two questions are deliberately not asked. The version and platform come
 * from the running install (the backend fills them in), because a user
 * should not have to look either up to report a bug. Priority is owner
 * triage: it is a judgement about a queue the filer cannot see.
 */

/** One entry from `support_context().ticket_types`. */
export interface TicketTypeOption {
  value: string;
  description: string;
  /** GitHub's prefilled form for this type -- the tokenless fallback. */
  url?: string;
}

/** What the backend reports for a ticket it created. */
export interface FiledTicket {
  number: number;
  url: string;
  title: string;
}

export interface SupportTicketDialogProps {
  /** The type the filer picked in the menu; changeable in the form. */
  initialType: string;
  ticketTypes: TicketTypeOption[];
  /** Where to POST -- reported by the backend so the two cannot drift. */
  submitRoute: string;
  /** This install's version/platform, shown so the filer sees what is sent. */
  environment?: { version?: string; platform?: string; python?: string } | null;
  /** GitHub's prefilled form, offered only when filing here fails. */
  fallbackUrl?: string | null;
  onClose: () => void;
}

const OVERLAY_STYLE: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  background: 'rgba(0, 0, 0, 0.45)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  zIndex: 1000,
  padding: 16,
};

const PANEL_STYLE: React.CSSProperties = {
  background: 'var(--background)',
  color: 'var(--foreground)',
  border: '1px solid var(--border-light)',
  borderRadius: 10,
  width: 'min(560px, 100%)',
  maxHeight: '90vh',
  overflowY: 'auto',
  padding: 20,
  boxShadow: '0 10px 40px rgba(0, 0, 0, 0.3)',
};

const FIELD_STYLE: React.CSSProperties = {
  width: '100%',
  padding: '8px 10px',
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
  marginBottom: 4,
};

export default function SupportTicketDialog({
  initialType,
  ticketTypes,
  submitRoute,
  environment,
  fallbackUrl,
  onClose,
}: SupportTicketDialogProps) {
  const [ticketType, setTicketType] = useState(initialType);
  const [summary, setSummary] = useState('');
  const [description, setDescription] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filed, setFiled] = useState<FiledTicket | null>(null);

  // Escape closes, from either screen. A modal that traps someone who
  // changed their mind about reporting a problem is its own support ticket.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  const submit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setSubmitting(true);
      setError(null);
      try {
        const res = await fetch(submitRoute, {
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
          // Never `data.detail` directly: the backend's error envelope and
          // FastAPI's own refusals are different shapes, and interpolating
          // either renders as [object Object] (#3831).
          throw new Error(apiErrorText(data, `HTTP ${res.status}`));
        }
        setFiled({ number: data.number, url: data.url, title: data.title });
      } catch (err) {
        setError(errorMessage(err));
      } finally {
        setSubmitting(false);
      }
    },
    [description, submitRoute, summary, ticketType]
  );

  const canSubmit = summary.trim().length > 0 && description.trim().length > 0 && !submitting;

  return (
    <div
      style={OVERLAY_STYLE}
      // Clicking the backdrop dismisses; clicking inside the panel must not.
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={filed ? 'Support ticket filed' : 'File a support ticket'}
        style={PANEL_STYLE}
        onClick={(event) => event.stopPropagation()}
      >
        {filed ? (
          <div>
            <h2 style={{ margin: '0 0 8px', fontSize: 18 }}>🎉 Thanks — your ticket is filed!</h2>
            <p style={{ margin: '0 0 12px', fontSize: 14, lineHeight: 1.5 }}>
              It went straight to the nyxGPT support queue, so you did not have to leave the
              app. Someone will pick it up from there — you can follow along or add anything
              you forgot on the ticket itself.
            </p>
            <p style={{ margin: '0 0 16px', fontSize: 14 }}>
              <a
                href={filed.url}
                target="_blank"
                rel="noopener noreferrer"
                style={{ color: 'var(--accent, #3b82f6)', fontWeight: 600 }}
              >
                Ticket #{filed.number}: {filed.title}
              </a>
            </p>
            <button type="button" onClick={onClose} style={primaryButtonStyle(true)}>
              Back to chat
            </button>
          </div>
        ) : (
          <form onSubmit={submit}>
            <h2 style={{ margin: '0 0 4px', fontSize: 18 }}>File a support ticket</h2>
            <p style={{ margin: '0 0 16px', fontSize: 13, opacity: 0.75, lineHeight: 1.5 }}>
              Describe what happened in your own words — nothing about nyxGPT&apos;s internals
              is needed. nyxGPT files this for you and shows you the ticket when it is done.
            </p>

            <div style={{ marginBottom: 12 }}>
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
                    {option.value} — {option.description}
                  </option>
                ))}
              </select>
            </div>

            <div style={{ marginBottom: 12 }}>
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

            <div style={{ marginBottom: 12 }}>
              <label htmlFor="support-ticket-description" style={LABEL_STYLE}>
                What happened?
              </label>
              <textarea
                id="support-ticket-description"
                value={description}
                maxLength={8000}
                rows={7}
                onChange={(event) => setDescription(event.target.value)}
                placeholder={
                  'What were you trying to do, what did you expect, and what happened ' +
                  'instead? Paste any error message you saw.'
                }
                style={{ ...FIELD_STYLE, resize: 'vertical' }}
              />
            </div>

            {/* Shown, not asked: the install already knows both, and the
                filer should be able to see exactly what gets attached. */}
            <p style={{ margin: '0 0 16px', fontSize: 12, opacity: 0.7 }}>
              Sent with your report: nyxGPT {environment?.version ?? 'unknown version'} on{' '}
              {environment?.platform ?? 'an unknown platform'}.
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
                {/* The one place GitHub's own form is still offered: filing
                    from here did not work, so a path that does not depend on
                    this install beats telling the user to give up. */}
                {fallbackUrl && (
                  <a
                    href={fallbackUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{ display: 'inline-block', marginTop: 8 }}
                  >
                    Report it on GitHub instead
                  </a>
                )}
              </div>
            )}

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                type="button"
                onClick={onClose}
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
                Cancel
              </button>
              <button type="submit" disabled={!canSubmit} style={primaryButtonStyle(canSubmit)}>
                {submitting ? 'Filing…' : 'File ticket'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

/** The filled button, dimmed when there is nothing to submit yet. */
function primaryButtonStyle(enabled: boolean): React.CSSProperties {
  return {
    padding: '8px 14px',
    borderRadius: 6,
    border: 'none',
    background: 'var(--accent, #3b82f6)',
    color: '#fff',
    fontSize: 14,
    fontWeight: 600,
    cursor: enabled ? 'pointer' : 'not-allowed',
    opacity: enabled ? 1 : 0.6,
  };
}
