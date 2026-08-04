'use client';

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type SecretEntry = {
  section: string;
  key: string;
  full_key: string;
  label: string;
  description: string;
  obtain: string;
  can_generate: boolean;
  set: boolean;
  masked: string | null;
};

type SyncResult = {
  ok: boolean;
  message: string;
  details: string;
};

const cardStyle: React.CSSProperties = {
  padding: '1rem 1.25rem',
  borderRadius: '0.5rem',
  border: '1px solid var(--border-color)',
  background: 'var(--background-secondary)',
  marginBottom: '1rem',
};

function isUrl(value: string): boolean {
  return value.startsWith('http://') || value.startsWith('https://');
}

export default function SecretsSetupPage() {
  const [secrets, setSecrets] = useState<SecretEntry[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [fieldMessages, setFieldMessages] = useState<Record<string, string>>({});

  const [syncing, setSyncing] = useState(false);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [syncResults, setSyncResults] = useState<SyncResult[] | null>(null);
  const [syncWasDryRun, setSyncWasDryRun] = useState(false);

  const loadSecrets = useCallback(async () => {
    setError(null);
    try {
      const res = await fetch('/api/v1/config/secrets', { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      }
      setSecrets(data.secrets as SecretEntry[]);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSecrets();
  }, [loadSecrets]);

  async function handleSave(secret: SecretEntry, generate: boolean) {
    setSaving(secret.full_key);
    setFieldErrors((prev) => ({ ...prev, [secret.full_key]: '' }));
    setFieldMessages((prev) => ({ ...prev, [secret.full_key]: '' }));
    try {
      const body = generate
        ? { section: secret.section, key: secret.key, generate: true }
        : { section: secret.section, key: secret.key, value: drafts[secret.full_key] };
      const res = await fetch('/api/v1/config/secrets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      }
      setSecrets(data.secrets as SecretEntry[]);
      setDrafts((prev) => ({ ...prev, [secret.full_key]: '' }));
      setFieldMessages((prev) => ({ ...prev, [secret.full_key]: `Saved (${data.masked}).` }));
    } catch (e: unknown) {
      setFieldErrors((prev) => ({
        ...prev,
        [secret.full_key]: e instanceof Error ? e.message : String(e),
      }));
    } finally {
      setSaving(null);
    }
  }

  async function handleSync(dryRun: boolean) {
    setSyncing(true);
    setSyncError(null);
    setSyncResults(null);
    try {
      const res = await fetch('/api/v1/config/secrets/sync', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dry_run: dryRun }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      }
      setSyncResults(data.results as SyncResult[]);
      setSyncWasDryRun(dryRun);
    } catch (e: unknown) {
      setSyncError(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  }

  if (loading) {
    return (
      <div style={{ padding: '2rem', textAlign: 'center' }}>
        <LoadingSpinner size="large" />
        <p style={{ marginTop: '1rem' }}>Loading guided secrets setup...</p>
      </div>
    );
  }

  return (
    <div style={{ padding: '2rem', maxWidth: '900px', margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ fontSize: '2rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
          Guided Secrets Setup
        </h1>
        <p style={{ color: 'var(--foreground-muted)', marginBottom: 8 }}>
          These secrets are write-once at the service that issues them -- once set here,
          config.ini is their canonical copy. Values are never shown in cleartext once saved.
        </p>
        <a href="/admin/dashboard" style={{ color: '#0066cc', textDecoration: 'none' }}>
          ← Back to Admin Dashboard
        </a>
      </div>

      {error && (
        <div style={{ marginBottom: '1rem' }}>
          <ErrorMessage message={error} onRetry={loadSecrets} />
        </div>
      )}

      {secrets &&
        secrets.map((secret) => (
          <div key={secret.full_key} style={cardStyle}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
              <strong>{secret.label}</strong>
              <code style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)' }}>
                {secret.full_key}
              </code>
            </div>
            <p style={{ fontSize: '0.875rem', margin: '0.5rem 0' }}>{secret.description}</p>
            <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.75rem' }}>
              Where to get it:{' '}
              {isUrl(secret.obtain) ? (
                <a href={secret.obtain} target="_blank" rel="noopener noreferrer" style={{ color: '#0066cc' }}>
                  {secret.obtain}
                </a>
              ) : (
                secret.obtain
              )}
            </p>
            <p style={{ fontSize: '0.8rem', marginBottom: '0.75rem' }}>
              Status:{' '}
              {secret.set ? (
                <strong>Set ({secret.masked})</strong>
              ) : (
                <span style={{ color: 'var(--foreground-muted)' }}>Not set</span>
              )}
            </p>

            <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', alignItems: 'center' }}>
              <input
                type="password"
                placeholder={secret.set ? 'Enter a new value to replace it' : 'Paste value here'}
                value={drafts[secret.full_key] || ''}
                onChange={(e) => setDrafts((prev) => ({ ...prev, [secret.full_key]: e.target.value }))}
                style={{
                  flex: '1 1 260px',
                  padding: '0.5rem',
                  borderRadius: '0.375rem',
                  border: '1px solid var(--border-color)',
                  background: 'var(--background)',
                  color: 'var(--foreground)',
                }}
              />
              <button
                onClick={() => handleSave(secret, false)}
                disabled={saving === secret.full_key || !drafts[secret.full_key]}
                style={{
                  padding: '0.5rem 1rem',
                  borderRadius: '0.375rem',
                  border: '1px solid var(--border-color)',
                  cursor: 'pointer',
                }}
              >
                {saving === secret.full_key ? 'Saving…' : 'Save'}
              </button>
              {secret.can_generate && (
                <button
                  onClick={() => handleSave(secret, true)}
                  disabled={saving === secret.full_key}
                  style={{
                    padding: '0.5rem 1rem',
                    borderRadius: '0.375rem',
                    border: '1px solid var(--border-color)',
                    cursor: 'pointer',
                  }}
                >
                  {saving === secret.full_key ? 'Generating…' : 'Generate for me'}
                </button>
              )}
            </div>

            {fieldErrors[secret.full_key] && (
              <div style={{ marginTop: '0.5rem' }}>
                <ErrorMessage message={fieldErrors[secret.full_key]} />
              </div>
            )}
            {fieldMessages[secret.full_key] && (
              <p style={{ marginTop: '0.5rem', fontSize: '0.8rem', color: 'var(--foreground-muted)' }}>
                {fieldMessages[secret.full_key]}
              </p>
            )}
          </div>
        ))}

      <div style={{ ...cardStyle, marginTop: '2rem' }}>
        <strong>Sync to GitHub Actions secrets</strong>
        <p style={{ fontSize: '0.875rem', margin: '0.5rem 0' }}>
          Pushes the write-once secrets declared for sync (Slack bot token, agent PATs) from
          config.ini into this repo&apos;s GitHub Actions secrets -- one direction only. Values are
          never shown; only secret names and success/failure.
        </p>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button
            onClick={() => handleSync(true)}
            disabled={syncing}
            style={{
              padding: '0.5rem 1rem',
              borderRadius: '0.375rem',
              border: '1px solid var(--border-color)',
              cursor: 'pointer',
            }}
          >
            {syncing ? 'Working…' : 'Preview (dry run)'}
          </button>
          <button
            onClick={() => handleSync(false)}
            disabled={syncing}
            style={{
              padding: '0.5rem 1rem',
              borderRadius: '0.375rem',
              border: '1px solid var(--border-color)',
              cursor: 'pointer',
            }}
          >
            {syncing ? 'Working…' : 'Sync now'}
          </button>
        </div>

        {syncError && (
          <div style={{ marginTop: '0.75rem' }}>
            <ErrorMessage message={syncError} />
          </div>
        )}

        {syncResults && (
          <div style={{ marginTop: '0.75rem' }}>
            <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.4rem' }}>
              {syncWasDryRun ? 'Dry run -- nothing was pushed:' : 'Sync results:'}
            </p>
            <ul style={{ paddingLeft: '1.2rem', fontSize: '0.85rem' }}>
              {syncResults.map((r, i) => (
                <li key={i} style={{ color: r.ok ? 'inherit' : 'var(--error-text)' }}>
                  {r.ok ? '✅' : '❌'} {r.message}
                  {r.details && (
                    <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)' }}>{r.details}</div>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
