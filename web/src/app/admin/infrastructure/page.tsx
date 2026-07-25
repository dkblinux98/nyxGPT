'use client';

import { useCallback, useEffect, useState } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type InfraStatus = {
  terraform: {
    deployed: boolean;
    containers: Record<string, string>;
  };
  kubernetes: {
    available: boolean;
    deployed: boolean;
    namespace: string;
    pods: string[];
  };
};

type OpsStepResult = {
  ok: boolean;
  message: string;
  details: string;
};

type OpsActionResponse = {
  ok: boolean;
  results: OpsStepResult[];
};

const boxStyle: React.CSSProperties = {
  padding: '1.5rem',
  backgroundColor: 'var(--background-secondary)',
  borderRadius: '0.5rem',
  border: '1px solid var(--border-color)',
};

const buttonStyle: React.CSSProperties = {
  padding: '0.5rem 1rem',
  border: '1px solid var(--border-color)',
  borderRadius: '0.375rem',
  fontSize: '0.875rem',
  fontWeight: 600,
  cursor: 'pointer',
};

const primaryButtonStyle: React.CSSProperties = {
  ...buttonStyle,
  backgroundColor: '#3b82f6',
  color: 'white',
  border: 'none',
};

const destructiveButtonStyle: React.CSSProperties = {
  ...buttonStyle,
  backgroundColor: '#ef4444',
  color: 'white',
  border: 'none',
};

function disabled(style: React.CSSProperties): React.CSSProperties {
  return { ...style, opacity: 0.6, cursor: 'not-allowed' };
}

async function parseOpsResponse(res: Response): Promise<OpsActionResponse> {
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || data.detail || `HTTP ${res.status}`);
  }
  return data as OpsActionResponse;
}

function StepResults({ results }: { results: OpsStepResult[] }) {
  return (
    <ul style={{ listStyle: 'none', padding: 0, margin: '0.75rem 0 0', display: 'flex', flexDirection: 'column', gap: 4 }}>
      {results.map((r, idx) => (
        <li
          key={idx}
          style={{
            fontSize: '0.8rem',
            padding: '0.4rem 0.6rem',
            borderRadius: '0.25rem',
            background: 'var(--background)',
            border: `1px solid ${r.ok ? 'var(--border-color)' : '#ef4444'}`,
          }}
        >
          <span style={{ color: r.ok ? '#22c55e' : '#ef4444', fontWeight: 600 }}>
            {r.ok ? '[OK]' : '[FAIL]'}
          </span>{' '}
          {r.message}
          {r.details && (
            <div style={{ marginTop: 4, color: 'var(--foreground-muted)', whiteSpace: 'pre-wrap' }}>
              {r.details}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

export default function InfrastructurePage() {
  const [status, setStatus] = useState<InfraStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [terraformApiKey, setTerraformApiKey] = useState('');
  const [terraformBusy, setTerraformBusy] = useState<'install' | 'down' | null>(null);
  const [terraformResult, setTerraformResult] = useState<OpsActionResponse | null>(null);
  const [terraformError, setTerraformError] = useState<string | null>(null);

  const [kubernetesApiKey, setKubernetesApiKey] = useState('');
  const [kubernetesBusy, setKubernetesBusy] = useState<'install' | 'down' | null>(null);
  const [kubernetesResult, setKubernetesResult] = useState<OpsActionResponse | null>(null);
  const [kubernetesError, setKubernetesError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    setError(null);
    try {
      const res = await fetch('/api/v1/infra/status', { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      }
      setStatus(data);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  async function handleTerraformInstall() {
    if (
      !confirm(
        'Install the local Terraform stack? This installs terraform (if missing), runs init/plan/apply, ' +
          'and brings up Ollama/Cassandra/API/web containers. It refuses to run if a native/Compose stack ' +
          'already owns those ports.'
      )
    ) {
      return;
    }
    setTerraformBusy('install');
    setTerraformError(null);
    setTerraformResult(null);
    try {
      const res = await fetch('/api/v1/infra/terraform/install', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: terraformApiKey || undefined }),
      });
      const result = await parseOpsResponse(res);
      setTerraformResult(result);
      await loadStatus();
    } catch (e: unknown) {
      setTerraformError(e instanceof Error ? e.message : String(e));
    } finally {
      setTerraformBusy(null);
    }
  }

  async function handleTerraformDown() {
    if (!confirm('Destroy the Terraform-managed stack (terraform destroy)? This removes all its containers and data.')) {
      return;
    }
    setTerraformBusy('down');
    setTerraformError(null);
    setTerraformResult(null);
    try {
      const res = await fetch('/api/v1/infra/terraform/down', { method: 'POST' });
      const result = await parseOpsResponse(res);
      setTerraformResult(result);
      await loadStatus();
    } catch (e: unknown) {
      setTerraformError(e instanceof Error ? e.message : String(e));
    } finally {
      setTerraformBusy(null);
    }
  }

  async function handleKubernetesInstall() {
    if (
      !confirm(
        'Deploy nyxgpt-api to the local Kubernetes cluster? This builds/loads the nyxgpt-api:local image ' +
          'and applies the k8s/ kustomization (requires a reachable local cluster).'
      )
    ) {
      return;
    }
    setKubernetesBusy('install');
    setKubernetesError(null);
    setKubernetesResult(null);
    try {
      const res = await fetch('/api/v1/infra/kubernetes/install', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: kubernetesApiKey || undefined }),
      });
      const result = await parseOpsResponse(res);
      setKubernetesResult(result);
      await loadStatus();
    } catch (e: unknown) {
      setKubernetesError(e instanceof Error ? e.message : String(e));
    } finally {
      setKubernetesBusy(null);
    }
  }

  async function handleKubernetesDown() {
    if (!confirm('Remove the nyxgpt namespace and all its Kubernetes resources?')) {
      return;
    }
    setKubernetesBusy('down');
    setKubernetesError(null);
    setKubernetesResult(null);
    try {
      const res = await fetch('/api/v1/infra/kubernetes/down', { method: 'POST' });
      const result = await parseOpsResponse(res);
      setKubernetesResult(result);
      await loadStatus();
    } catch (e: unknown) {
      setKubernetesError(e instanceof Error ? e.message : String(e));
    } finally {
      setKubernetesBusy(null);
    }
  }

  if (loading) {
    return (
      <div style={{ padding: '2rem', textAlign: 'center' }}>
        <LoadingSpinner size="large" />
        <p style={{ marginTop: '1rem' }}>Loading infrastructure status...</p>
      </div>
    );
  }

  return (
    <div style={{ padding: '2rem', maxWidth: '900px', margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ fontSize: '2rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
          Terraform &amp; Kubernetes Deploys
        </h1>
        <p style={{ color: 'var(--foreground-muted)', marginBottom: 8 }}>
          One-command local infrastructure deploys — no raw <code>brew</code>/<code>terraform</code>/
          <code>kubectl</code> commands.
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
        Only local deployment (<code>--local</code>) is implemented today. This is the precursor to a
        future cloud target, not an alternative to it — see <code>docs/terraform.md</code> and{' '}
        <code>docs/kubernetes.md</code>.
      </div>

      {error && (
        <div style={{ marginBottom: '1.5rem' }}>
          <ErrorMessage message={error} onRetry={loadStatus} />
        </div>
      )}

      <div style={{ display: 'grid', gap: '1.5rem' }}>
        {/* --- Terraform --- */}
        <div style={boxStyle}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>Terraform</h2>
            {status && (
              <span
                style={{
                  fontSize: '0.75rem',
                  fontWeight: 600,
                  padding: '2px 8px',
                  borderRadius: 999,
                  background: status.terraform.deployed ? '#22c55e' : 'var(--background)',
                  color: status.terraform.deployed ? 'white' : 'var(--foreground-muted)',
                  border: status.terraform.deployed ? 'none' : '1px solid var(--border-color)',
                }}
              >
                {status.terraform.deployed ? 'DEPLOYED' : 'NOT DEPLOYED'}
              </span>
            )}
          </div>

          {status && Object.keys(status.terraform.containers).length > 0 && (
            <ul style={{ listStyle: 'none', padding: 0, margin: '0 0 1rem', fontSize: '0.875rem' }}>
              {Object.entries(status.terraform.containers).map(([component, state]) => (
                <li key={component} style={{ display: 'flex', justifyContent: 'space-between', padding: '2px 0' }}>
                  <span>{component}</span>
                  <span style={{ color: state === 'running' ? '#22c55e' : 'var(--foreground-muted)' }}>{state}</span>
                </li>
              ))}
            </ul>
          )}

          <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', marginBottom: '0.75rem', flexWrap: 'wrap' }}>
            <input
              type="password"
              placeholder="Auth API key (optional — auto-generated if blank)"
              value={terraformApiKey}
              onChange={(e) => setTerraformApiKey(e.target.value)}
              disabled={terraformBusy !== null}
              style={{
                flex: '1 1 260px',
                padding: '0.4rem 0.6rem',
                borderRadius: '0.375rem',
                border: '1px solid var(--border-color)',
                background: 'var(--background)',
                color: 'var(--foreground)',
                fontSize: '0.85rem',
              }}
            />
          </div>

          <div style={{ display: 'flex', gap: '0.75rem' }}>
            <button
              onClick={handleTerraformInstall}
              disabled={terraformBusy !== null}
              style={terraformBusy !== null ? disabled(primaryButtonStyle) : primaryButtonStyle}
            >
              {terraformBusy === 'install' ? 'Installing…' : 'Install'}
            </button>
            <button
              onClick={handleTerraformDown}
              disabled={terraformBusy !== null}
              style={terraformBusy !== null ? disabled(destructiveButtonStyle) : destructiveButtonStyle}
            >
              {terraformBusy === 'down' ? 'Destroying…' : 'Destroy'}
            </button>
            <button
              onClick={loadStatus}
              disabled={terraformBusy !== null}
              style={terraformBusy !== null ? disabled(buttonStyle) : { ...buttonStyle, background: 'var(--background)' }}
            >
              Refresh
            </button>
          </div>

          {terraformError && (
            <div style={{ marginTop: '0.75rem' }}>
              <ErrorMessage message={terraformError} />
            </div>
          )}
          {terraformResult && <StepResults results={terraformResult.results} />}
        </div>

        {/* --- Kubernetes --- */}
        <div style={boxStyle}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '0.75rem' }}>
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold' }}>Kubernetes</h2>
            {status && (
              <span
                style={{
                  fontSize: '0.75rem',
                  fontWeight: 600,
                  padding: '2px 8px',
                  borderRadius: 999,
                  background: status.kubernetes.deployed ? '#22c55e' : 'var(--background)',
                  color: status.kubernetes.deployed ? 'white' : 'var(--foreground-muted)',
                  border: status.kubernetes.deployed ? 'none' : '1px solid var(--border-color)',
                }}
              >
                {status.kubernetes.deployed ? 'DEPLOYED' : 'NOT DEPLOYED'}
              </span>
            )}
          </div>

          {status && !status.kubernetes.available && (
            <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)', marginBottom: '1rem' }}>
              kubectl not found on this host — install it before deploying.
            </p>
          )}

          {status && status.kubernetes.pods.length > 0 && (
            <ul style={{ listStyle: 'none', padding: 0, margin: '0 0 1rem', fontSize: '0.8rem', fontFamily: 'monospace' }}>
              {status.kubernetes.pods.map((line, idx) => (
                <li key={idx} style={{ padding: '2px 0' }}>
                  {line}
                </li>
              ))}
            </ul>
          )}

          <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', marginBottom: '0.75rem', flexWrap: 'wrap' }}>
            <input
              type="password"
              placeholder="Auth API key (optional — auto-generated if blank)"
              value={kubernetesApiKey}
              onChange={(e) => setKubernetesApiKey(e.target.value)}
              disabled={kubernetesBusy !== null}
              style={{
                flex: '1 1 260px',
                padding: '0.4rem 0.6rem',
                borderRadius: '0.375rem',
                border: '1px solid var(--border-color)',
                background: 'var(--background)',
                color: 'var(--foreground)',
                fontSize: '0.85rem',
              }}
            />
          </div>

          <div style={{ display: 'flex', gap: '0.75rem' }}>
            <button
              onClick={handleKubernetesInstall}
              disabled={kubernetesBusy !== null}
              style={kubernetesBusy !== null ? disabled(primaryButtonStyle) : primaryButtonStyle}
            >
              {kubernetesBusy === 'install' ? 'Installing…' : 'Install'}
            </button>
            <button
              onClick={handleKubernetesDown}
              disabled={kubernetesBusy !== null}
              style={kubernetesBusy !== null ? disabled(destructiveButtonStyle) : destructiveButtonStyle}
            >
              {kubernetesBusy === 'down' ? 'Removing…' : 'Remove'}
            </button>
            <button
              onClick={loadStatus}
              disabled={kubernetesBusy !== null}
              style={kubernetesBusy !== null ? disabled(buttonStyle) : { ...buttonStyle, background: 'var(--background)' }}
            >
              Refresh
            </button>
          </div>

          {kubernetesError && (
            <div style={{ marginTop: '0.75rem' }}>
              <ErrorMessage message={kubernetesError} />
            </div>
          )}
          {kubernetesResult && <StepResults results={kubernetesResult.results} />}
        </div>
      </div>
    </div>
  );
}
