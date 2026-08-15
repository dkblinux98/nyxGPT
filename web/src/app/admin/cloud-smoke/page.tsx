'use client';

// SRE/admin surface for the containerized cloud artifact-install smoke (#3784)
// -- the dashboard counterpart of `nyxgpt cloud smoke --container`.
//
// This page has a real button, unlike the AWS cloud pages next to it (#3514
// made that surface status-plus-CLI-pointers because a deploy spends money
// and needs credentials). A run here builds a local container, installs the
// published artifact into it and throws the container away: no AWS account,
// no charges, nothing created outside Docker. What it costs is *time* -- tens
// of minutes -- so the POST starts a background run and this page polls the
// recorded result, which is the same record `nyxgpt cloud smoke --container
// --status` prints.
//
// The coverage gaps are rendered as prominently as the verdict, on purpose: a
// green run here proves the install path on a bare Amazon Linux 2023 machine,
// and nothing about Terraform, cloud-init, SSH or a real instance. Reading
// green as "the cloud path works" is exactly the mistake the five serial rc9
// defects were hiding behind.

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type SmokeStep = {
  step: string;
  [key: string]: unknown;
};

type SmokeResult = {
  passed: boolean;
  failure: string;
  failure_class: string;
  detail: string;
  expected_failure: boolean;
  version: string;
  base_image: string;
  steps: SmokeStep[];
  diagnostics: Record<string, string>;
  started_at: string;
  finished_at: string;
  duration_seconds: number;
  injected_faults: { fault: string; description: string; changed: boolean }[];
};

type SmokeStatus = {
  running: boolean;
  current: { started_at?: string; version?: string; faults?: string[] };
  last_result: SmokeResult | Record<string, never>;
  docker_available: boolean;
  docker_detail: string;
  base_image: string;
  faults: { fault: string; description: string }[];
  coverage_gaps: string[];
  commands: Record<string, string>;
  docs: string;
};

const boxStyle: React.CSSProperties = {
  padding: '1rem',
  border: '1px solid var(--border-color)',
  borderRadius: '0.5rem',
  background: 'var(--background-secondary)',
};

const commandStyle: React.CSSProperties = {
  display: 'block',
  padding: '0.4rem 0.6rem',
  marginTop: '0.35rem',
  borderRadius: '0.25rem',
  background: 'var(--background)',
  border: '1px solid var(--border-color)',
  fontSize: '0.8rem',
  fontFamily: 'monospace',
  overflowX: 'auto',
};

// While a run is in flight the panel polls often enough to feel live without
// hammering an endpoint that shells out to `docker info` on every call.
const POLL_INTERVAL_MS = 15000;

function hasResult(result: SmokeStatus['last_result']): result is SmokeResult {
  return Boolean(result && Object.keys(result).length > 0);
}

// The API's error envelope is `{"error": {"code", "message", ...}}` and
// FastAPI's own refusals are `{"detail": "..."}`; a 409 from this endpoint --
// "a run is already in flight", "Docker is not usable here" -- arrives in the
// first shape. Reading `data.error` directly rendered the banner as
// "[object Object]", i.e. it hid exactly the messages the operator needs.
function errorText(data: unknown, fallback: string): string {
  if (data && typeof data === 'object') {
    const record = data as Record<string, unknown>;
    const nested = record.error;
    if (nested && typeof nested === 'object') {
      const message = (nested as Record<string, unknown>).message;
      if (typeof message === 'string') return message;
    }
    if (typeof nested === 'string') return nested;
    if (typeof record.detail === 'string') return record.detail;
  }
  return fallback;
}

export default function CloudArtifactSmokePage() {
  const [status, setStatus] = useState<SmokeStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [version, setVersion] = useState('');
  const [fault, setFault] = useState('');

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await fetch('/api/v1/ops/cloud-artifact-smoke', { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(errorText(data, `HTTP ${res.status}`));
      }
      setStatus(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Polling only while a run is in flight: the record is static otherwise.
  useEffect(() => {
    if (!status?.running) return;
    const timer = setInterval(() => void load(), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [status?.running, load]);

  const startRun = useCallback(async () => {
    setStarting(true);
    setNotice(null);
    setError(null);
    try {
      const res = await fetch('/api/v1/ops/cloud-artifact-smoke', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          version: version.trim() || undefined,
          inject: fault ? [fault] : undefined,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(errorText(data, `HTTP ${res.status}`));
      }
      setNotice(
        `Run started at ${data.started_at}. It takes tens of minutes; this page polls for the verdict.`
      );
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  }, [version, fault, load]);

  if (loading) {
    return (
      <div style={{ padding: '2rem', textAlign: 'center' }}>
        <LoadingSpinner size="large" />
        <p style={{ marginTop: '1rem' }}>Loading the cloud artifact smoke status...</p>
      </div>
    );
  }

  const result = status && hasResult(status.last_result) ? status.last_result : null;

  return (
    <div style={{ padding: '2rem', maxWidth: '900px', margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ fontSize: '2rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
          Cloud Artifact Smoke
        </h1>
        <p style={{ color: 'var(--foreground-muted)', marginBottom: 8 }}>
          Installs nyxGPT from a published artifact onto a <strong>bare Amazon Linux 2023</strong>{' '}
          container — the AMI&apos;s Python 3.9, no node, no docker, no git — and requires the
          services to come up. It is the install half of the cloud path, verified without spending
          anything.
        </p>
        <a href="/admin/dashboard" style={{ color: '#0066cc', textDecoration: 'none' }}>
          ← Back to Admin Dashboard
        </a>
      </div>

      {error && (
        <div style={{ marginBottom: '1.5rem' }}>
          <ErrorMessage message={error} onRetry={load} />
        </div>
      )}

      {notice && (
        <div
          style={{
            marginBottom: '1.5rem',
            padding: '0.75rem 1rem',
            borderRadius: '0.375rem',
            background: 'var(--info-bg)',
            border: '1px solid var(--border-color)',
            fontSize: '0.875rem',
          }}
        >
          {notice}
        </div>
      )}

      <div style={{ display: 'grid', gap: '1.5rem' }}>
        {/* --- Run control --- */}
        <div style={boxStyle}>
          <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
            Run a smoke
          </h2>
          {!status?.docker_available && (
            <p style={{ fontSize: '0.85rem', color: '#ef4444', marginBottom: '0.5rem' }}>
              Docker is not usable on the machine running the API — {status?.docker_detail}. The
              smoke builds and boots a container, so it cannot run here.
            </p>
          )}
          <div
            style={{
              display: 'flex',
              gap: '0.5rem',
              flexWrap: 'wrap',
              alignItems: 'center',
              marginBottom: '0.5rem',
            }}
          >
            <input
              value={version}
              onChange={(e) => setVersion(e.target.value)}
              placeholder="version (blank = latest published)"
              aria-label="Published nyxGPT version to install"
              style={{
                flex: '1 1 240px',
                padding: '0.4rem 0.6rem',
                border: '1px solid var(--border-color)',
                borderRadius: '0.375rem',
                background: 'var(--background)',
                fontSize: '0.85rem',
              }}
            />
            <select
              value={fault}
              onChange={(e) => setFault(e.target.value)}
              aria-label="Fault to inject"
              style={{
                padding: '0.4rem 0.6rem',
                border: '1px solid var(--border-color)',
                borderRadius: '0.375rem',
                background: 'var(--background)',
                fontSize: '0.85rem',
              }}
            >
              <option value="">no fault injected</option>
              {(status?.faults ?? []).map((f) => (
                <option key={f.fault} value={f.fault}>
                  inject {f.fault} (must fail)
                </option>
              ))}
            </select>
            <button
              onClick={startRun}
              disabled={starting || Boolean(status?.running) || !status?.docker_available}
              style={{
                padding: '0.45rem 0.9rem',
                border: '1px solid var(--border-color)',
                borderRadius: '0.375rem',
                fontSize: '0.85rem',
                fontWeight: 600,
                background: 'var(--background)',
                cursor:
                  starting || status?.running || !status?.docker_available
                    ? 'not-allowed'
                    : 'pointer',
                opacity: starting || status?.running || !status?.docker_available ? 0.6 : 1,
              }}
            >
              {status?.running ? 'Run in flight…' : starting ? 'Starting…' : 'Run smoke'}
            </button>
            <button
              onClick={load}
              style={{
                padding: '0.45rem 0.9rem',
                border: '1px solid var(--border-color)',
                borderRadius: '0.375rem',
                fontSize: '0.85rem',
                background: 'var(--background)',
                cursor: 'pointer',
              }}
            >
              Refresh
            </button>
          </div>
          {status?.running && (
            <p style={{ fontSize: '0.85rem' }}>
              Started {status.current.started_at} (version {status.current.version}
              {status.current.faults?.length ? `, injecting ${status.current.faults.join(', ')}` : ''}
              ). A full run takes tens of minutes.
            </p>
          )}
          <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginTop: '0.5rem' }}>
            The same run from a terminal:
          </p>
          {Object.entries(status?.commands ?? {}).map(([key, command]) => (
            <code key={key} style={commandStyle}>
              {command}
            </code>
          ))}
          <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginTop: '0.5rem' }}>
            Injecting a fault reintroduces a known defect class and inverts the verdict: the run
            passes only if the smoke <em>fails</em>, which is how we know a green run is not green
            by luck.
          </p>
        </div>

        {/* --- Last result --- */}
        <div style={boxStyle}>
          <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
            Last run
          </h2>
          {!result && (
            <p style={{ fontSize: '0.9rem' }}>
              No run has been recorded on this machine yet.
            </p>
          )}
          {result && (
            <>
              <p style={{ fontSize: '1rem', marginBottom: '0.5rem' }}>
                <strong style={{ color: result.passed ? '#16a34a' : '#ef4444' }}>
                  {result.passed ? 'PASSED' : 'FAILED'}
                </strong>{' '}
                — {result.detail}
              </p>
              <p style={{ fontSize: '0.85rem', color: 'var(--foreground-muted)' }}>
                version {result.version} on {result.base_image}, finished {result.finished_at} in{' '}
                {Math.round(result.duration_seconds)}s
                {result.expected_failure ? ' (fault-injected run: failure is the pass condition)' : ''}
              </p>
              {result.failure_class && (
                <p style={{ fontSize: '0.85rem', marginTop: '0.5rem' }}>
                  <strong>Defect class:</strong> {result.failure_class}
                </p>
              )}
              <div style={{ marginTop: '0.75rem', display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
                {(result.steps ?? []).map((step) => (
                  <span
                    key={step.step}
                    style={{
                      padding: '0.2rem 0.5rem',
                      borderRadius: '999px',
                      border: '1px solid var(--border-color)',
                      fontSize: '0.75rem',
                    }}
                  >
                    {step.step}
                  </span>
                ))}
              </div>
              {Object.entries(result.diagnostics ?? {})
                .filter(([, text]) => Boolean(text))
                .map(([label, text]) => (
                  <details key={label} style={{ marginTop: '0.75rem' }}>
                    <summary style={{ cursor: 'pointer', fontSize: '0.85rem' }}>{label}</summary>
                    <pre
                      style={{
                        fontSize: '0.75rem',
                        overflowX: 'auto',
                        padding: '0.5rem',
                        background: 'var(--background)',
                        borderRadius: '0.25rem',
                      }}
                    >
                      {text}
                    </pre>
                  </details>
                ))}
            </>
          )}
        </div>

        {/* --- Coverage gaps --- */}
        <div style={boxStyle}>
          <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
            What a green run does <em>not</em> cover
          </h2>
          <ul style={{ fontSize: '0.85rem', paddingLeft: '1.2rem', display: 'grid', gap: '0.4rem' }}>
            {(status?.coverage_gaps ?? []).map((gap) => (
              <li key={gap}>{gap}</li>
            ))}
          </ul>
          <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)', marginTop: '0.5rem' }}>
            Full detail: <code>{status?.docs}</code>.
          </p>
        </div>
      </div>
    </div>
  );
}
