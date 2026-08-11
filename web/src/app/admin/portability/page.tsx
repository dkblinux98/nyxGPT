'use client';

// SRE/admin surface for the repo-less portability matrix and the Phase 6
// clean-machine acceptance sequence (P6-16, #3516) -- the dashboard
// counterpart of `nyxgpt ops portability`.
//
// **Read-only, and not merely by policy.** The #3514 decision makes the cloud
// lifecycle surface status-plus-CLI-pointers; this page is a step further,
// because the matrix is a property of the product (which artifacts are
// published, which commands are wrapped, which targets still need a checkout)
// rather than of this machine. There is no action to expose. Every row's
// install/operate/teardown commands are rendered from the backend's own
// matrix, so the page can never drift from what the CLI accepts.
//
// The gaps are the point of the page, not an embarrassment to hide: a target
// that still needs a repo checkout fails CLAUDE.md's Repo-less Portability
// requirement, and the capstone is not acceptable while any row is red.

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type MatrixCheck = {
  check: string;
  passed: boolean;
  skipped: boolean;
  detail: string;
};

type MatrixTarget = {
  key: string;
  name: string;
  artifact: string;
  install: string[];
  operate: string[];
  teardown: string;
  status: 'ci-verified' | 'acceptance' | 'gap';
  evidence: string[];
  notes: string;
  gaps: string[];
  checks: MatrixCheck[];
  invariants_passed: boolean;
  acceptance_ready: boolean;
};

type AcceptanceStep = {
  step: string;
  command: string;
  expect: string;
};

// `GET /api/v1/ops/release-candidate` -- the dashboard half of
// `nyxgpt release publish` (#3727). Acceptance installs come from PyPI, so
// testing the release-branch tip repo-less needs a published pre-release --
// a nightly `dev` build or an on-demand `rc`. This panel says which one to
// pin and whether another can be cut.
type ReleaseCandidatePlan = {
  branch: string;
  channel: string;
  release: string;
  declared_version: string;
  published_rcs: string[];
  published_dev_builds: string[];
  next_rc_version: string;
  next_dev_version: string;
  version: string;
  is_prerelease: boolean;
  publishable: boolean;
  blockers: string[];
  guardrails: string[];
  pypi_lookup_error: string;
  workflow: string;
  docs: string;
  commands: Record<string, string>;
};

type PortabilityReport = {
  targets: MatrixTarget[];
  acceptance_sequence: AcceptanceStep[];
  commands: Record<string, string>;
  checkout: string;
  summary: {
    total: number;
    acceptance_ready: number;
    invariants_failed: number;
    open_gaps: number;
    windows_in_scope: boolean;
  };
  acceptance_ready: boolean;
};

const boxStyle: React.CSSProperties = {
  padding: '1.5rem',
  backgroundColor: 'var(--background-secondary)',
  borderRadius: '0.5rem',
  border: '1px solid var(--border-color)',
};

const STATUS_LABELS: Record<MatrixTarget['status'], string> = {
  'ci-verified': 'Verified in CI',
  acceptance: 'Owner acceptance',
  gap: 'Gap',
};

function badgeStyle(ok: boolean): React.CSSProperties {
  return {
    fontSize: '0.75rem',
    fontWeight: 600,
    padding: '2px 8px',
    borderRadius: 999,
    background: ok ? '#22c55e' : '#ef4444',
    color: 'white',
    whiteSpace: 'nowrap',
  };
}

const commandStyle: React.CSSProperties = {
  display: 'block',
  fontFamily: 'monospace',
  fontSize: '0.8rem',
  padding: '2px 0',
};

function CommandList({ label, commands }: { label: string; commands: string[] }) {
  if (commands.length === 0) return null;
  return (
    <div style={{ marginTop: '0.5rem' }}>
      <div style={{ fontSize: '0.75rem', color: 'var(--foreground-muted)', fontWeight: 600 }}>
        {label}
      </div>
      {commands.map((command) => (
        <code key={command} style={commandStyle}>
          {command}
        </code>
      ))}
    </div>
  );
}

export default function PortabilityPage() {
  const [report, setReport] = useState<PortabilityReport | null>(null);
  const [rc, setRc] = useState<ReleaseCandidatePlan | null>(null);
  const [rcError, setRcError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The RC plan reaches PyPI, so it loads on its own and never blocks (or
  // fails) the matrix above it -- an unreachable PyPI must not hide which
  // targets are repo-less.
  const loadReleaseCandidate = useCallback(async () => {
    setRcError(null);
    try {
      const res = await fetch('/api/v1/ops/release-candidate', { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      }
      setRc(data);
    } catch (e: unknown) {
      setRcError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const loadReport = useCallback(async () => {
    setRefreshing(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/ops/portability', { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      }
      setReport(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void loadReport();
    void loadReleaseCandidate();
  }, [loadReport, loadReleaseCandidate]);

  if (loading) {
    return (
      <div style={{ padding: '2rem', textAlign: 'center' }}>
        <LoadingSpinner size="large" />
        <p style={{ marginTop: '1rem' }}>Loading the portability matrix...</p>
      </div>
    );
  }

  return (
    <div style={{ padding: '2rem', maxWidth: '900px', margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ fontSize: '2rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
          Portability &amp; Acceptance
        </h1>
        <p style={{ color: 'var(--foreground-muted)', marginBottom: 8 }}>
          Which deployment targets install and operate with <strong>no repo checkout</strong>, and
          the clean-machine sequence that accepts Phase 6.
        </p>
        <a href="/admin/dashboard" style={{ color: '#0066cc', textDecoration: 'none' }}>
          ← Back to Admin Dashboard
        </a>
      </div>

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
        This page reports; it never changes anything. The matrix describes the product — which
        artifacts are published and which commands are wrapped — not this machine, so every row
        here is a pointer to a <code>nyxgpt</code> command you run in a terminal. The same report
        is available as <code>{report?.commands?.report ?? 'nyxgpt ops portability'}</code>, and{' '}
        <code>{report?.commands?.strict ?? 'nyxgpt ops portability --strict'}</code> exits non-zero
        while any target still needs a checkout. Windows is explicitly out of scope.
      </div>

      {error && (
        <div style={{ marginBottom: '1.5rem' }}>
          <ErrorMessage message={error} onRetry={loadReport} />
        </div>
      )}

      {report && (
        <div style={{ display: 'grid', gap: '1.5rem' }}>
          {/* --- Summary --- */}
          <div style={boxStyle}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                marginBottom: '0.5rem',
              }}
            >
              <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>Repo-less portability</h2>
              <button
                onClick={loadReport}
                disabled={refreshing}
                title="Re-read the matrix -- does not change anything"
                style={{
                  padding: '0.4rem 0.8rem',
                  border: '1px solid var(--border-color)',
                  borderRadius: '0.375rem',
                  fontSize: '0.8rem',
                  fontWeight: 600,
                  cursor: refreshing ? 'not-allowed' : 'pointer',
                  background: 'var(--background)',
                  opacity: refreshing ? 0.6 : 1,
                }}
              >
                {refreshing ? 'Refreshing…' : 'Refresh'}
              </button>
            </div>
            <p style={{ fontSize: '1rem', marginBottom: '0.5rem' }}>
              <strong>
                {report.summary.acceptance_ready}/{report.summary.total}
              </strong>{' '}
              targets installable and operable without a repo checkout
              {report.summary.open_gaps > 0 && <> — {report.summary.open_gaps} open gap(s)</>}
              {report.summary.invariants_failed > 0 && (
                <> — {report.summary.invariants_failed} invariant failure(s)</>
              )}
              .
            </p>
            {!report.acceptance_ready && (
              <p style={{ fontSize: '0.85rem', color: '#ef4444' }}>
                The Phase 6 capstone portability criterion is not met while any gap is open. Each
                gap below is a product gap, not a documentation one.
              </p>
            )}
          </div>

          {/* --- One box per target --- */}
          {report.targets.map((target) => (
            <div key={target.key} style={boxStyle}>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: '1rem',
                  marginBottom: '0.25rem',
                }}
              >
                <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>{target.name}</h2>
                <span style={badgeStyle(target.acceptance_ready)}>
                  {STATUS_LABELS[target.status]}
                </span>
              </div>
              <p style={{ fontSize: '0.8rem', color: 'var(--foreground-muted)' }}>
                Published artifact: {target.artifact}
              </p>

              <CommandList label="Install (clean machine)" commands={target.install} />
              <CommandList label="Operate" commands={target.operate} />
              <CommandList label="Tear down" commands={[target.teardown]} />

              <div style={{ marginTop: '0.75rem' }}>
                {target.checks.map((check) => (
                  <div key={check.check} style={{ fontSize: '0.8rem', padding: '1px 0' }}>
                    <span
                      style={{
                        color: check.skipped
                          ? 'var(--foreground-muted)'
                          : check.passed
                            ? '#22c55e'
                            : '#ef4444',
                        fontWeight: 600,
                      }}
                    >
                      {check.skipped ? 'skipped' : check.passed ? 'pass' : 'FAIL'}
                    </span>{' '}
                    <strong>{check.check}</strong> — {check.detail}
                  </div>
                ))}
              </div>

              {target.gaps.length > 0 && (
                <ul
                  style={{
                    marginTop: '0.75rem',
                    marginBottom: 0,
                    paddingLeft: '1.25rem',
                    fontSize: '0.8rem',
                    color: '#ef4444',
                  }}
                >
                  {target.gaps.map((gap) => (
                    <li key={gap}>{gap}</li>
                  ))}
                </ul>
              )}

              {target.notes && (
                <p
                  style={{
                    marginTop: '0.75rem',
                    marginBottom: 0,
                    fontSize: '0.8rem',
                    color: 'var(--foreground-muted)',
                  }}
                >
                  {target.notes}
                </p>
              )}

              {target.evidence.length > 0 && (
                <p
                  style={{
                    marginTop: '0.5rem',
                    marginBottom: 0,
                    fontSize: '0.75rem',
                    color: 'var(--foreground-muted)',
                  }}
                >
                  Evidence: {target.evidence.join(', ')}
                </p>
              )}
            </div>
          ))}

          {/* --- The acceptance run --- */}
          <div style={boxStyle}>
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
              Clean-machine acceptance sequence
            </h2>
            <p style={{ fontSize: '0.85rem', color: 'var(--foreground-muted)' }}>
              Run in order on a machine that has never seen this repository. The AWS steps create
              and then destroy real, billed infrastructure, which is exactly why they are
              deliberate terminal commands and not buttons. Full runbook:{' '}
              <code>docs/portability-matrix.md</code>.
            </p>
            <ol style={{ paddingLeft: '1.25rem', fontSize: '0.85rem', marginBottom: 0 }}>
              {report.acceptance_sequence.map((step) => (
                <li key={step.step} style={{ marginTop: '0.5rem' }}>
                  <code style={{ fontFamily: 'monospace' }}>{step.command}</code>
                  <div style={{ color: 'var(--foreground-muted)' }}>→ {step.expect}</div>
                </li>
              ))}
            </ol>
          </div>

          {/* --- Acceptance-testing unreleased code (#3727) --- */}
          <div style={boxStyle}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: '1rem',
                marginBottom: '0.5rem',
              }}
            >
              <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>
                PyPI builds — acceptance-test the tip
              </h2>
              {rc && (
                <span style={badgeStyle(rc.publishable)}>
                  {rc.publishable ? 'ready to cut' : 'blocked'}
                </span>
              )}
            </div>
            <p style={{ fontSize: '0.85rem', color: 'var(--foreground-muted)' }}>
              Every install above comes from PyPI, so acceptance testing can only reach code that
              has been published. One pipeline publishes the release-branch tip on three channels —
              a nightly <code>dev</code> build, an on-demand <code>rc</code>, and the release itself
              (<code>stable</code>, run only by the owner&apos;s ceremony). Dev and rc builds are
              pre-releases a clean machine installs by exact pin. Publishing is an owner action, so
              it runs from a terminal or on the schedule, never from this page.
            </p>

            {rcError && (
              <p style={{ fontSize: '0.85rem', color: '#ef4444' }}>
                Could not load the publish plan: {rcError}
              </p>
            )}

            {rc && (
              <>
                <div style={{ fontSize: '0.85rem', marginTop: '0.5rem' }}>
                  <div>
                    Release line <strong>{rc.release}</strong> from branch{' '}
                    <code>{rc.branch}</code>
                  </div>
                  <div>
                    Published RCs:{' '}
                    {rc.published_rcs.length > 0 ? rc.published_rcs.join(', ') : 'none yet'}
                  </div>
                  <div>
                    Nightly dev builds:{' '}
                    {rc.published_dev_builds.length > 0
                      ? rc.published_dev_builds.join(', ')
                      : 'none yet'}
                  </div>
                  <div>
                    Next <code>{rc.channel}</code> build: <strong>{rc.version}</strong>
                    {rc.is_prerelease && (
                      <span style={{ color: 'var(--foreground-muted)' }}>
                        {' '}
                        — a pre-release, so <code>pip install nyxgpt</code> never resolves to it
                      </span>
                    )}
                  </div>
                </div>

                {rc.pypi_lookup_error && (
                  <p style={{ fontSize: '0.8rem', color: '#ef4444', marginTop: '0.5rem' }}>
                    PyPI lookup failed, so the next build number is unknown: {rc.pypi_lookup_error}
                  </p>
                )}

                {rc.blockers.length > 0 && (
                  <ul
                    style={{
                      marginTop: '0.75rem',
                      marginBottom: 0,
                      paddingLeft: '1.25rem',
                      fontSize: '0.8rem',
                      color: '#ef4444',
                    }}
                  >
                    {rc.blockers.map((blocker) => (
                      <li key={blocker}>{blocker}</li>
                    ))}
                  </ul>
                )}

                <CommandList label="Cut a build now" commands={[rc.commands.publish]} />
                <CommandList
                  label="Point an acceptance install at it"
                  commands={[rc.commands.install, rc.commands.user_data, rc.commands.deploy]}
                />

                <ul
                  style={{
                    marginTop: '0.75rem',
                    marginBottom: 0,
                    paddingLeft: '1.25rem',
                    fontSize: '0.8rem',
                    color: 'var(--foreground-muted)',
                  }}
                >
                  {rc.guardrails.map((guardrail) => (
                    <li key={guardrail}>{guardrail}</li>
                  ))}
                </ul>

                <p
                  style={{
                    marginTop: '0.75rem',
                    marginBottom: 0,
                    fontSize: '0.75rem',
                    color: 'var(--foreground-muted)',
                  }}
                >
                  Workflow: <code>.github/workflows/{rc.workflow}</code> (nightly schedule + manual
                  dispatch only) — runbook:{' '}
                  <code>{rc.docs}</code>
                </p>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
