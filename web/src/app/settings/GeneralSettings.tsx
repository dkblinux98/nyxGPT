'use client';

import { useEffect, useState } from 'react';
import { useTheme } from '../../contexts/ThemeContext';
import LoadingSpinner from '../../components/LoadingSpinner';
import ErrorMessage from '../../components/ErrorMessage';

type Info = {
  ollama_base_url: string;
  default_model: string;
  sessions_dir: string;
  release_version: string | null;
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
            <div style={rowStyle}>
              <span style={labelStyle}>Version</span>
              <span style={valueStyle}>{info.release_version || 'unknown'}</span>
            </div>
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
