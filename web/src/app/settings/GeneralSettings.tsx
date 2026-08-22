'use client';

import { useEffect, useState } from 'react';
import { useTheme } from '../../contexts/ThemeContext';
import LoadingSpinner from '../../components/LoadingSpinner';
import ErrorMessage from '../../components/ErrorMessage';
import { channelLabel, describeStackVersions } from '../lib/version';

type Info = {
  ollama_base_url: string;
  default_model: string;
  sessions_dir: string;
  /** Installed package version actually running (e.g. "3.0.0"). */
  release_version: string | null;
  /** Agent tooling's configured release branch -- not the running version. */
  release_branch?: string | null;
  /** Which tier `release_version` is: `stable`, `rc`, `dev`, `unknown` (#3982). */
  release_channel?: string | null;
  /** The web tier's own version, stamped in by the `/api/info` route (#3982). */
  web_version?: string | null;
  /** How `web_version` was established (#3982). */
  web_version_source?: string | null;
};

/**
 * Plain-English account of how the web tier's version was established
 * (#3982). Shown because "unknown" on its own invites the operator to
 * distrust the whole surface; naming the reason tells them whether to set
 * `NYXGPT_WEB_VERSION` or whether they are simply on a dev server.
 */
const WEB_VERSION_SOURCE_LABEL: Record<string, string> = {
  env: 'from NYXGPT_WEB_VERSION',
  'homebrew-keg': 'from the installed Homebrew keg',
  'native-build': 'from the native install build directory',
  unknown: 'not identifiable on this install (set NYXGPT_WEB_VERSION to declare it)',
};

const cardStyle: React.CSSProperties = {
  padding: 20,
  background: 'var(--card-bg)',
  borderRadius: 8,
  border: '1px solid var(--border)',
};

const rowStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  marginBottom: 4,
};

const labelStyle: React.CSSProperties = { fontSize: 14, color: 'var(--text-secondary)' };
const valueStyle: React.CSSProperties = { fontSize: 14, fontWeight: 600, wordBreak: 'break-all' };

export default function GeneralSettings() {
  const { theme, setTheme } = useTheme();
  const [info, setInfo] = useState<Info | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Safe before `info` arrives: every field resolves to `unknown`, and the
  // rows that read it only render inside the `info &&` branch below.
  const stack = describeStackVersions({
    apiVersion: info?.release_version,
    webVersion: info?.web_version,
  });

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const res = await fetch('/api/info');
        if (!res.ok) {
          throw new Error(`Failed to fetch app info: HTTP ${res.status}`);
        }
        const data = await res.json();
        if (!cancelled) {
          setInfo(data);
          setError(null);
        }
      } catch (e: unknown) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 24 }}>
      {/* Appearance */}
      <div style={cardStyle}>
        <h3 style={{ margin: 0, marginBottom: 16, fontSize: 18, fontWeight: 600 }}>Appearance</h3>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={labelStyle}>Theme</span>
          <div style={{ display: 'flex', gap: 8 }} role="group" aria-label="Theme">
            <button
              onClick={() => setTheme('light')}
              aria-pressed={theme === 'light'}
              style={{
                background: theme === 'light' ? 'var(--button)' : 'transparent',
                color: theme === 'light' ? 'var(--button-text)' : 'var(--text)',
                border: '1px solid var(--border)',
                borderRadius: 6,
                padding: '8px 16px',
                cursor: 'pointer',
                fontSize: 14,
              }}
            >
              ☀️ Light
            </button>
            <button
              onClick={() => setTheme('dark')}
              aria-pressed={theme === 'dark'}
              style={{
                background: theme === 'dark' ? 'var(--button)' : 'transparent',
                color: theme === 'dark' ? 'var(--button-text)' : 'var(--text)',
                border: '1px solid var(--border)',
                borderRadius: 6,
                padding: '8px 16px',
                cursor: 'pointer',
                fontSize: 14,
              }}
            >
              🌙 Dark
            </button>
          </div>
        </div>
      </div>

      {/* About */}
      <div style={cardStyle}>
        <h3 style={{ margin: 0, marginBottom: 16, fontSize: 18, fontWeight: 600 }}>About</h3>

        {loading && (
          <div style={{ padding: 12 }}>
            <LoadingSpinner size="small" label="Loading app info..." />
          </div>
        )}

        {error && <ErrorMessage message={error} />}

        {info && (
          <div>
            {/* Both tiers, always -- not only when they disagree (#3982).
                The About surface is where an operator goes to answer "what
                am I running?", and an answer that silently collapses two
                separately installed services into one number is the defect
                this screen now exists to make impossible. */}
            <div style={rowStyle}>
              <span style={labelStyle}>API Version</span>
              <span style={valueStyle}>
                {stack.apiVersion || 'unknown'}
                {channelLabel(stack.apiChannel) ? ` (${channelLabel(stack.apiChannel)})` : ''}
              </span>
            </div>
            <div style={rowStyle}>
              <span style={labelStyle}>Web UI Version</span>
              <span style={valueStyle}>
                {stack.webVersion || 'unknown'}
                {channelLabel(stack.webChannel) ? ` (${channelLabel(stack.webChannel)})` : ''}
              </span>
            </div>
            <div style={rowStyle}>
              <span style={labelStyle}>Web UI Version Source</span>
              <span style={valueStyle}>
                {WEB_VERSION_SOURCE_LABEL[info.web_version_source ?? 'unknown'] ??
                  info.web_version_source}
              </span>
            </div>
            {stack.mismatch && (
              <div
                role="status"
                data-testid="settings-version-mismatch"
                style={{
                  margin: '8px 0',
                  padding: '8px 12px',
                  borderRadius: 6,
                  border: '1px solid var(--error-text)',
                  background: 'var(--error-bg)',
                  color: 'var(--error-text)',
                  fontSize: 13,
                }}
              >
                ⚠ {stack.detail}. The two tiers are separate installs; reconcile them
                with <code>nyxgpt ops install</code>.
              </div>
            )}
            <div style={rowStyle}>
              <span style={labelStyle}>Default Model</span>
              <span style={valueStyle}>{info.default_model}</span>
            </div>
            <div style={rowStyle}>
              <span style={labelStyle}>Ollama Base URL</span>
              <span style={valueStyle}>{info.ollama_base_url}</span>
            </div>
            <div style={rowStyle}>
              <span style={labelStyle}>Sessions Directory</span>
              <span style={valueStyle}>{info.sessions_dir}</span>
            </div>
          </div>
        )}

        <div style={{ marginTop: 16, fontSize: 13, color: 'var(--text-secondary)' }}>
          To change the default model, RAG, or logging configuration, use the{' '}
          <a href="/admin" style={{ color: 'var(--primary)' }}>
            Configuration Wizard
          </a>
          .
        </div>
      </div>
    </div>
  );
}
