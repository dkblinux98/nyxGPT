'use client';

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type FieldMeta = {
  key: string;
  label: string;
  description: string;
  obtain: string;
  secret: boolean;
};

type CloudReference = {
  profile: string;
  region: string;
  credentials_source: string;
};

type DestinationStatus = {
  set: boolean;
  masked_access_key_id: string | null;
  available?: boolean;
};

type SecretStoreEntry = {
  key: string;
  label: string;
  description: string;
  value: string;
};

type AwsCredentialsStatus = {
  fields: FieldMeta[];
  reference: CloudReference;
  profile_file_status: DestinationStatus;
  keychain_status: DestinationStatus;
  secret_store: SecretStoreEntry[];
};

type Destination = 'profile' | 'keychain' | 'ambient';

type ErrorPayload = { error?: string; detail?: string };

const DEFAULT_PROFILE = 'nyxgpt';
const DEFAULT_REGION = 'us-east-1';
const DEFAULT_DESTINATION: Destination = 'profile';

const cardStyle: React.CSSProperties = {
  padding: '1rem 1.25rem',
  borderRadius: '0.5rem',
  border: '1px solid var(--border-color)',
  background: 'var(--background-secondary)',
  marginBottom: '1rem',
};

const inputStyle: React.CSSProperties = {
  padding: '0.5rem',
  borderRadius: '0.375rem',
  border: '1px solid var(--border-color)',
  background: 'var(--background)',
  color: 'var(--foreground)',
  width: '100%',
};

const buttonStyle: React.CSSProperties = {
  padding: '0.5rem 1rem',
  borderRadius: '0.375rem',
  border: '1px solid var(--border-color)',
  cursor: 'pointer',
};

const DESTINATIONS: { value: Destination; label: string; description: string }[] = [
  {
    value: 'profile',
    label: 'AWS CLI profile file',
    description: 'Written to ~/.aws/credentials -- same as `aws configure --profile`.',
  },
  {
    value: 'keychain',
    label: 'OS keychain',
    description: 'Stored via the `keyring` package instead of a plaintext file.',
  },
  {
    value: 'ambient',
    label: 'Already configured elsewhere',
    description: 'An existing profile, instance role, SSO session, or environment variables. Nothing is entered below.',
  },
];

function fieldByKey(fields: FieldMeta[], key: string): FieldMeta | undefined {
  return fields.find((f) => f.key === key);
}

/** Pick the most specific message a failed config response offers. */
function errorText(data: ErrorPayload, status: number): string {
  return data.error || data.detail || `HTTP ${status}`;
}

/** Render a thrown value as a display string, whatever it turned out to be. */
function messageOf(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export default function AwsCredentialsSetupPage() {
  const [status, setStatus] = useState<AwsCredentialsStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [profile, setProfile] = useState('');
  const [region, setRegion] = useState('');
  const [destination, setDestination] = useState<Destination>(DEFAULT_DESTINATION);
  const [accessKeyId, setAccessKeyId] = useState('');
  const [secretAccessKey, setSecretAccessKey] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);

  const [secretStore, setSecretStore] = useState<SecretStoreEntry[]>([]);
  const [storeDrafts, setStoreDrafts] = useState<Record<string, string>>({});
  const [storeSaving, setStoreSaving] = useState(false);
  const [storeError, setStoreError] = useState<string | null>(null);
  const [storeMessage, setStoreMessage] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    setError(null);
    try {
      const res = await fetch('/api/v1/config/aws-credentials', { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(errorText(data, res.status));
      }
      const s = data as AwsCredentialsStatus;
      setStatus(s);
      setProfile(s.reference.profile || DEFAULT_PROFILE);
      setRegion(s.reference.region || DEFAULT_REGION);
      setDestination((s.reference.credentials_source || DEFAULT_DESTINATION) as Destination);
      setSecretStore(s.secret_store);
      setStoreDrafts(Object.fromEntries(s.secret_store.map((entry) => [entry.key, entry.value])));
    } catch (e: unknown) {
      setError(messageOf(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  async function handleSaveCredentials() {
    setSaving(true);
    setSaveError(null);
    setSaveMessage(null);
    try {
      const body: Record<string, unknown> = { destination, profile, region };
      if (destination !== 'ambient') {
        body.access_key_id = accessKeyId;
        body.secret_access_key = secretAccessKey;
      }
      const res = await fetch('/api/v1/config/aws-credentials', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(errorText(data, res.status));
      }
      setStatus(data as AwsCredentialsStatus);
      setAccessKeyId('');
      setSecretAccessKey('');
      setSaveMessage('Saved.');
    } catch (e: unknown) {
      setSaveError(messageOf(e));
    } finally {
      setSaving(false);
    }
  }

  async function handleSaveSecretStore() {
    setStoreSaving(true);
    setStoreError(null);
    setStoreMessage(null);
    try {
      const res = await fetch('/api/v1/config/aws-credentials/secret-store', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(storeDrafts),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(errorText(data, res.status));
      }
      setSecretStore(data.secret_store as SecretStoreEntry[]);
      setStoreMessage('Secret store reference saved.');
    } catch (e: unknown) {
      setStoreError(messageOf(e));
    } finally {
      setStoreSaving(false);
    }
  }

  if (loading) {
    return (
      <div style={{ padding: '2rem', textAlign: 'center' }}>
        <LoadingSpinner size="large" />
        <p style={{ marginTop: '1rem' }}>Loading AWS credentials setup...</p>
      </div>
    );
  }

  const profileField = status ? fieldByKey(status.fields, 'profile') : undefined;
  const regionField = status ? fieldByKey(status.fields, 'region') : undefined;
  const accessKeyField = status ? fieldByKey(status.fields, 'access_key_id') : undefined;
  const secretKeyField = status ? fieldByKey(status.fields, 'secret_access_key') : undefined;

  return (
    <div style={{ padding: '2rem', maxWidth: '900px', margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ fontSize: '2rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
          AWS Credentials Setup
        </h1>
        <p style={{ color: 'var(--foreground-muted)', marginBottom: 8 }}>
          The AWS identity nyxGPT uses for its own AWS API calls (cloud allow-ip, cloud deploy,
          SSM/Secrets Manager resolution). The access key pair is never written to config.ini --
          it is routed to ~/.aws/credentials, the OS keychain, or left alone if already available.
        </p>
        <a href="/admin/dashboard" style={{ color: '#0066cc', textDecoration: 'none' }}>
          ← Back to Admin Dashboard
        </a>
      </div>

      {error && (
        <div style={{ marginBottom: '1rem' }}>
          <ErrorMessage message={error} onRetry={loadStatus} />
        </div>
      )}

      {status && (
        <div style={cardStyle}>
          <strong>Profile &amp; region</strong>

          {profileField && (
            <div style={{ margin: '0.75rem 0' }}>
              <label htmlFor="aws-profile" style={{ display: 'block', fontSize: '0.875rem', marginBottom: '0.25rem' }}>
                {profileField.label}
              </label>
              <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.4rem' }}>
                {profileField.description}
              </p>
              <input
                id="aws-profile"
                type="text"
                value={profile}
                onChange={(e) => setProfile(e.target.value)}
                style={inputStyle}
              />
            </div>
          )}

          {regionField && (
            <div style={{ margin: '0.75rem 0' }}>
              <label htmlFor="aws-region" style={{ display: 'block', fontSize: '0.875rem', marginBottom: '0.25rem' }}>
                {regionField.label}
              </label>
              <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.4rem' }}>
                {regionField.description}
              </p>
              <input
                id="aws-region"
                type="text"
                placeholder="us-east-1"
                value={region}
                onChange={(e) => setRegion(e.target.value)}
                style={inputStyle}
              />
            </div>
          )}

          <div style={{ margin: '1rem 0 0.5rem' }}>
            <strong style={{ fontSize: '0.9rem' }}>How should nyxGPT get AWS credentials?</strong>
          </div>
          {DESTINATIONS.map((d) => (
            <label key={d.value} style={{ display: 'flex', gap: '0.5rem', alignItems: 'flex-start', marginBottom: '0.5rem' }}>
              <input
                type="radio"
                name="destination"
                value={d.value}
                checked={destination === d.value}
                onChange={() => setDestination(d.value)}
                style={{ marginTop: '0.2rem' }}
              />
              <span>
                <strong style={{ fontSize: '0.875rem' }}>{d.label}</strong>
                <br />
                <span style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)' }}>{d.description}</span>
              </span>
            </label>
          ))}

          {destination !== 'ambient' && accessKeyField && secretKeyField && (
            <div style={{ marginTop: '1rem', display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
              <div>
                <label htmlFor="aws-access-key-id" style={{ display: 'block', fontSize: '0.875rem', marginBottom: '0.25rem' }}>
                  {accessKeyField.label}
                </label>
                <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.4rem' }}>
                  Where to get it: {accessKeyField.obtain}
                </p>
                <input
                  id="aws-access-key-id"
                  type="password"
                  placeholder="Paste value here"
                  value={accessKeyId}
                  onChange={(e) => setAccessKeyId(e.target.value)}
                  style={inputStyle}
                />
              </div>
              <div>
                <label htmlFor="aws-secret-access-key" style={{ display: 'block', fontSize: '0.875rem', marginBottom: '0.25rem' }}>
                  {secretKeyField.label}
                </label>
                <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.4rem' }}>
                  Where to get it: {secretKeyField.obtain}
                </p>
                <input
                  id="aws-secret-access-key"
                  type="password"
                  placeholder="Paste value here"
                  value={secretAccessKey}
                  onChange={(e) => setSecretAccessKey(e.target.value)}
                  style={inputStyle}
                />
              </div>
            </div>
          )}

          <div style={{ marginTop: '1rem', fontSize: '0.8rem' }}>
            <p>
              ~/.aws/credentials:{' '}
              {status.profile_file_status.set ? (
                <strong>Set ({status.profile_file_status.masked_access_key_id})</strong>
              ) : (
                <span style={{ color: 'var(--foreground-muted)' }}>Not set</span>
              )}
            </p>
            <p>
              OS keychain:{' '}
              {status.keychain_status.available === false ? (
                <span style={{ color: 'var(--foreground-muted)' }}>keyring not installed</span>
              ) : status.keychain_status.set ? (
                <strong>Set ({status.keychain_status.masked_access_key_id})</strong>
              ) : (
                <span style={{ color: 'var(--foreground-muted)' }}>Not set</span>
              )}
            </p>
          </div>

          <button
            onClick={handleSaveCredentials}
            disabled={saving}
            style={{ ...buttonStyle, marginTop: '0.5rem' }}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>

          {saveError && (
            <div style={{ marginTop: '0.5rem' }}>
              <ErrorMessage message={saveError} />
            </div>
          )}
          {saveMessage && (
            <p style={{ marginTop: '0.5rem', fontSize: '0.8rem', color: 'var(--foreground-muted)' }}>
              {saveMessage}
            </p>
          )}
        </div>
      )}

      {status && (
        <div style={cardStyle}>
          <strong>Cloud secret store reference</strong>
          <p style={{ fontSize: '0.875rem', margin: '0.5rem 0' }}>
            Where nyxGPT&apos;s own secrets ([auth] api_key, [openai] api_key, [github] pat) are
            resolved from on a cloud deploy. These aren&apos;t secret values themselves -- the
            application secrets stay in SSM/Secrets Manager.
          </p>

          {secretStore.map((entry) => (
            <div key={entry.key} style={{ margin: '0.75rem 0' }}>
              <label
                htmlFor={`secret-store-${entry.key}`}
                style={{ display: 'block', fontSize: '0.875rem', marginBottom: '0.25rem' }}
              >
                {entry.label}
              </label>
              <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginBottom: '0.4rem' }}>
                {entry.description}
              </p>
              <input
                id={`secret-store-${entry.key}`}
                type="text"
                value={storeDrafts[entry.key] ?? entry.value}
                onChange={(e) => setStoreDrafts((prev) => ({ ...prev, [entry.key]: e.target.value }))}
                style={inputStyle}
              />
            </div>
          ))}

          <button onClick={handleSaveSecretStore} disabled={storeSaving} style={buttonStyle}>
            {storeSaving ? 'Saving…' : 'Save secret store reference'}
          </button>

          {storeError && (
            <div style={{ marginTop: '0.5rem' }}>
              <ErrorMessage message={storeError} />
            </div>
          )}
          {storeMessage && (
            <p style={{ marginTop: '0.5rem', fontSize: '0.8rem', color: 'var(--foreground-muted)' }}>
              {storeMessage}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
