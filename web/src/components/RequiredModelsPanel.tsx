'use client';

// Required-model readiness for the SRE/admin dashboard (#3824).
//
// The stack can report every service healthy and still fail the user's first
// chat message, if Ollama never downloaded the configured chat model -- and
// the same for the first RAG-enabled message and the embedding model. That is
// what `nyxgpt ops install` now pulls before it reports the stack up, so this
// panel normally reads "ready"; it exists for the machine where it does not
// (a deleted model, an Ollama pointed at another store, a model configured
// after the last install), which an operator otherwise only discovers by
// sending a chat message and watching it fail.
//
// Pulling a model is a download into the LLM tier, not a change to the
// substrate this UI is served from, so offering the pull here is safe under
// the Definition of Done's observe-don't-operate rule.

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from './LoadingSpinner';
import ErrorMessage from './ErrorMessage';

export type RequiredModel = {
  role: string;
  model: string;
  setting: string;
  // null when Ollama could not be asked: "cannot tell" is not "missing".
  present: boolean | null;
};

export type RequiredModelsStatus = {
  base_url: string;
  reachable: boolean;
  error: string;
  models: RequiredModel[];
  ready: boolean;
  remediation: string;
};

const cardStyle: React.CSSProperties = {
  padding: '1.25rem',
  border: '1px solid var(--border)',
  borderRadius: 8,
  background: 'var(--background)',
};

function ReadinessBadge({ present }: { present: boolean | null }) {
  const label = present === null ? 'unknown' : present ? 'present' : 'missing';
  const ok = present === true;
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '4px 10px',
        borderRadius: 999,
        fontSize: 12,
        fontWeight: 600,
        background: ok ? 'var(--success-bg)' : 'var(--error-bg)',
        color: ok ? 'var(--success-text)' : 'var(--error-text)',
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: '50%',
          background: ok ? '#22c55e' : '#dc3545',
        }}
      />
      {label}
    </span>
  );
}

export default function RequiredModelsPanel() {
  const [status, setStatus] = useState<RequiredModelsStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pulling, setPulling] = useState<string | null>(null);
  const [pullError, setPullError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/models/required', { cache: 'no-store' });
      if (!res.ok) throw new Error(`Failed to load model readiness: HTTP ${res.status}`);
      setStatus(await res.json());
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const interval = setInterval(load, 30000);
    return () => clearInterval(interval);
  }, [load]);

  const pull = useCallback(
    async (model: string) => {
      setPulling(model);
      setPullError(null);
      try {
        const res = await fetch('/api/models', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model }),
        });
        if (!res.ok) throw new Error(`Pull failed: HTTP ${res.status}`);
        await load();
      } catch (e: unknown) {
        setPullError(e instanceof Error ? e.message : String(e));
      } finally {
        setPulling(null);
      }
    },
    [load]
  );

  return (
    <section style={cardStyle} aria-label="Required models">
      <h2 style={{ margin: 0, marginBottom: '1rem', fontSize: '1.1rem' }}>Required Models</h2>
      {loading && !status ? (
        <LoadingSpinner label="Loading model readiness..." />
      ) : error ? (
        <ErrorMessage
          title="Failed to load model readiness"
          message={error}
          onRetry={load}
          retrying={loading}
        />
      ) : status ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <p style={{ margin: 0, fontSize: 12, color: 'var(--muted-foreground)' }}>
            The chat and embedding models this install is configured to use, in Ollama at{' '}
            {status.base_url}. Both are required whether or not RAG is switched on, because RAG
            is a per-session toggle.
          </p>
          {!status.reachable && (
            <p style={{ margin: 0, fontSize: 13, color: 'var(--muted-foreground)' }}>
              Ollama did not answer, so readiness is unknown{status.error ? ` (${status.error})` : ''}.
            </p>
          )}
          {status.models.length === 0 ? (
            <p style={{ margin: 0, fontSize: 13, color: 'var(--muted-foreground)' }}>
              No models configured.
            </p>
          ) : (
            status.models.map((m) => (
              <div
                key={`${m.role}:${m.model}`}
                style={{ display: 'flex', flexDirection: 'column', gap: 4 }}
              >
                <ReadinessBadge present={m.present} />
                <span style={{ fontSize: 13 }}>
                  <strong>{m.model}</strong>
                  <span style={{ color: 'var(--muted-foreground)' }}>
                    {' '}
                    -- {m.role}, from {m.setting}
                  </span>
                </span>
                {m.present === false && (
                  <button
                    type="button"
                    onClick={() => pull(m.model)}
                    disabled={pulling !== null}
                    style={{
                      alignSelf: 'flex-start',
                      padding: '6px 12px',
                      fontSize: 13,
                      borderRadius: 6,
                      border: '1px solid var(--border)',
                      cursor: pulling !== null ? 'not-allowed' : 'pointer',
                    }}
                  >
                    {pulling === m.model ? `Pulling ${m.model}...` : `Pull ${m.model}`}
                  </button>
                )}
              </div>
            ))
          )}
          {pullError && (
            <p role="alert" style={{ margin: 0, fontSize: 13, color: 'var(--error-text)' }}>
              {pullError}
            </p>
          )}
          {status.remediation && (
            <p style={{ margin: 0, fontSize: 12, color: 'var(--muted-foreground)' }}>
              {status.remediation}
            </p>
          )}
        </div>
      ) : null}
    </section>
  );
}
