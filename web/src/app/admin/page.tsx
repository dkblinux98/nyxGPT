'use client';

import { useEffect, useState } from 'react';
import LoadingSpinner from '../../components/LoadingSpinner';
import ErrorMessage from '../../components/ErrorMessage';
import ResourceMetrics from '../../components/ResourceMetrics';

type SecretField = { set: boolean; masked: string | null };

interface SectionsData {
  nyxgpt: {
    default_model: string;
    chat_timeout_seconds: string;
    sessions_dir: string;
    vectorstore_dir: string;
  };
  logging: { level: string; dir: string };
  ollama: { base_url: string };
  api: { host: string; port: string };
  auth: { enabled: string; header: string; api_key: SecretField };
  rate_limit: { enabled: string };
  rag: {
    enable_chat_context: string;
    cassandra_hosts: string;
    cassandra_port: string;
    cassandra_keyspace: string;
    cassandra_table: string;
    embedding_model: string;
  };
  tracing: { enabled: string; service_name: string; otlp_endpoint: string };
  error_tracking: { enabled: string; dsn: SecretField; environment: string };
  monitoring: { enabled: string };
  log_aggregation: { enabled: string };
}

interface FormValues {
  nyxgpt: SectionsData['nyxgpt'];
  logging: SectionsData['logging'];
  ollama: SectionsData['ollama'];
  api: SectionsData['api'];
  auth: { enabled: string; header: string; api_key: string };
  rate_limit: SectionsData['rate_limit'];
  rag: SectionsData['rag'];
  tracing: SectionsData['tracing'];
  error_tracking: { enabled: string; dsn: string; environment: string };
  monitoring: SectionsData['monitoring'];
  log_aggregation: SectionsData['log_aggregation'];
}

interface SaveResult {
  applied: Record<string, Record<string, unknown>>;
  sections: SectionsData;
  restart_required: string[];
  observability_reconciled: boolean;
  observability_result: { ok: boolean; messages: string[] } | null;
}

type WizardStep = 'core' | 'rag' | 'api' | 'observability' | 'metrics' | 'summary';

const EMPTY_SECRET: SecretField = { set: false, masked: null };

function emptySections(): SectionsData {
  return {
    nyxgpt: { default_model: '', chat_timeout_seconds: '', sessions_dir: '', vectorstore_dir: '' },
    logging: { level: 'INFO', dir: '' },
    ollama: { base_url: '' },
    api: { host: '', port: '' },
    auth: { enabled: 'false', header: 'X-API-Key', api_key: EMPTY_SECRET },
    rate_limit: { enabled: 'false' },
    rag: {
      enable_chat_context: 'false',
      cassandra_hosts: '',
      cassandra_port: '',
      cassandra_keyspace: '',
      cassandra_table: '',
      embedding_model: '',
    },
    tracing: { enabled: 'false', service_name: '', otlp_endpoint: '' },
    error_tracking: { enabled: 'false', dsn: EMPTY_SECRET, environment: '' },
    monitoring: { enabled: 'false' },
    log_aggregation: { enabled: 'false' },
  };
}

function toFormValues(sections: SectionsData): FormValues {
  return {
    nyxgpt: { ...sections.nyxgpt },
    logging: { ...sections.logging },
    ollama: { ...sections.ollama },
    api: { ...sections.api },
    auth: { enabled: sections.auth.enabled, header: sections.auth.header, api_key: '' },
    rate_limit: { ...sections.rate_limit },
    rag: { ...sections.rag },
    tracing: { ...sections.tracing },
    error_tracking: {
      enabled: sections.error_tracking.enabled,
      dsn: '',
      environment: sections.error_tracking.environment,
    },
    monitoring: { ...sections.monitoring },
    log_aggregation: { ...sections.log_aggregation },
  };
}

function buildSavePayload(v: FormValues): Record<string, Record<string, unknown>> {
  const payload: Record<string, Record<string, unknown>> = {
    nyxgpt: {
      default_model: v.nyxgpt.default_model.trim(),
      chat_timeout_seconds: Number(v.nyxgpt.chat_timeout_seconds),
      sessions_dir: v.nyxgpt.sessions_dir.trim(),
      vectorstore_dir: v.nyxgpt.vectorstore_dir.trim(),
    },
    logging: { level: v.logging.level, dir: v.logging.dir.trim() },
    ollama: { base_url: v.ollama.base_url.trim() },
    api: { host: v.api.host.trim(), port: Number(v.api.port) },
    auth: { enabled: v.auth.enabled === 'true', header: v.auth.header.trim() },
    rate_limit: { enabled: v.rate_limit.enabled === 'true' },
    rag: {
      enable_chat_context: v.rag.enable_chat_context === 'true',
      cassandra_hosts: v.rag.cassandra_hosts.trim(),
      cassandra_port: Number(v.rag.cassandra_port),
      cassandra_keyspace: v.rag.cassandra_keyspace.trim(),
      cassandra_table: v.rag.cassandra_table.trim(),
      embedding_model: v.rag.embedding_model.trim(),
    },
    tracing: {
      enabled: v.tracing.enabled === 'true',
      service_name: v.tracing.service_name.trim(),
      otlp_endpoint: v.tracing.otlp_endpoint.trim(),
    },
    error_tracking: {
      enabled: v.error_tracking.enabled === 'true',
      environment: v.error_tracking.environment.trim(),
    },
    monitoring: { enabled: v.monitoring.enabled === 'true' },
    log_aggregation: { enabled: v.log_aggregation.enabled === 'true' },
  };
  if (v.auth.api_key.trim()) payload.auth.api_key = v.auth.api_key.trim();
  if (v.error_tracking.dsn.trim()) payload.error_tracking.dsn = v.error_tracking.dsn.trim();
  return payload;
}

/** Extracts a human-readable message from the API's `{error: {message, details}}` envelope. */
function extractErrorMessage(data: unknown, status: number): string {
  const err = (data as { error?: { message?: string; details?: { errors?: string[] } } } | null)
    ?.error;
  if (err?.details?.errors) {
    return err.details.errors.join('; ');
  }
  if (err?.message) {
    return err.message;
  }
  return `HTTP ${status}`;
}

const fieldLabelStyle = { display: 'block', marginBottom: 8, fontWeight: 600, fontSize: 14 } as const;
const inputStyle = {
  width: '100%',
  padding: '10px 12px',
  border: '1px solid var(--border)',
  borderRadius: 6,
  fontSize: 14,
  background: 'var(--background)',
  color: 'var(--foreground)',
} as const;

function TextInput({
  id,
  label,
  value,
  onChange,
  disabled,
  hint,
  type = 'text',
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
  hint?: string;
  type?: string;
}) {
  return (
    <div style={{ marginBottom: '1.25rem' }}>
      <label htmlFor={id} style={fieldLabelStyle}>
        {label}
      </label>
      <input
        id={id}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        style={inputStyle}
      />
      {hint && <div style={{ fontSize: 12, color: '#666', marginTop: 4 }}>{hint}</div>}
    </div>
  );
}

function CheckboxInput({
  id,
  label,
  description,
  checked,
  onChange,
  disabled,
}: {
  id: string;
  label: string;
  description: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled: boolean;
}) {
  return (
    <div style={{ marginBottom: '1.25rem' }}>
      <label
        htmlFor={id}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          cursor: 'pointer',
          padding: '1rem',
          border: '1px solid var(--border)',
          borderRadius: 6,
          background: 'var(--background)',
        }}
      >
        <input
          id={id}
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          disabled={disabled}
          style={{ width: 18, height: 18, cursor: 'pointer' }}
        />
        <div>
          <div style={{ fontWeight: 600, fontSize: 14 }}>{label}</div>
          <div style={{ fontSize: 12, color: '#666', marginTop: 4 }}>{description}</div>
        </div>
      </label>
    </div>
  );
}

function SelectInput({
  id,
  label,
  value,
  onChange,
  disabled,
  options,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
  options: { value: string; label: string }[];
}) {
  return (
    <div style={{ marginBottom: '1.25rem' }}>
      <label htmlFor={id} style={fieldLabelStyle}>
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        style={inputStyle}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

function SecretInput({
  id,
  label,
  value,
  onChange,
  disabled,
  set,
  masked,
  hint,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
  set: boolean;
  masked: string | null;
  hint?: string;
}) {
  return (
    <div style={{ marginBottom: '1.25rem' }}>
      <label htmlFor={id} style={fieldLabelStyle}>
        {label}
      </label>
      <input
        id={id}
        type="password"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        placeholder={set ? `Set (${masked}) -- leave blank to keep` : 'Not set'}
        autoComplete="new-password"
        style={inputStyle}
      />
      {hint && <div style={{ fontSize: 12, color: '#666', marginTop: 4 }}>{hint}</div>}
    </div>
  );
}

const LOG_LEVEL_OPTIONS = [
  { value: 'DEBUG', label: 'DEBUG - Verbose output for troubleshooting' },
  { value: 'INFO', label: 'INFO - Standard operational messages' },
  { value: 'WARNING', label: 'WARNING - Warning messages only' },
  { value: 'ERROR', label: 'ERROR - Error messages only' },
  { value: 'CRITICAL', label: 'CRITICAL - Critical failures only' },
];

export default function AdminPage() {
  const [sections, setSections] = useState<SectionsData>(emptySections());
  const [loading, setLoading] = useState(true);
  const [configError, setConfigError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [saveResult, setSaveResult] = useState<SaveResult | null>(null);
  const [restarting, setRestarting] = useState<string | null>(null);
  const [restartError, setRestartError] = useState<string | null>(null);
  const [restartedTargets, setRestartedTargets] = useState<string[]>([]);

  const [currentStep, setCurrentStep] = useState<WizardStep>('core');
  const [testingConnection, setTestingConnection] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);

  const [formValues, setFormValues] = useState<FormValues>(toFormValues(emptySections()));

  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);

  function updateSection<K extends keyof FormValues>(
    section: K,
    key: keyof FormValues[K],
    value: string
  ) {
    setFormValues((prev) => ({
      ...prev,
      [section]: { ...prev[section], [key]: value },
    }));
  }

  async function loadSections() {
    setLoading(true);
    setConfigError(null);
    try {
      const res = await fetch('/api/v1/config/sections');
      if (!res.ok) throw new Error(`Failed to load configuration: HTTP ${res.status}`);
      const data = await res.json();
      setSections(data.sections);
      setFormValues(toFormValues(data.sections));
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setConfigError(msg);
    } finally {
      setLoading(false);
    }
  }

  async function loadModels() {
    setLoadingModels(true);
    setModelsError(null);
    try {
      const res = await fetch('/api/models');
      if (!res.ok) throw new Error(`Failed to load models: HTTP ${res.status}`);
      const data = await res.json();
      setAvailableModels(data.models || []);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      console.error('Failed to load models:', e);
      setModelsError(msg);
      setAvailableModels([]);
    } finally {
      setLoadingModels(false);
    }
  }

  useEffect(() => {
    loadSections();
    loadModels();
  }, []);

  // Re-fetch the models list whenever the tab regains focus/visibility, so a
  // model pulled from the Manage Models page appears here without requiring
  // a manual reload.
  useEffect(() => {
    function handleFocus() {
      loadModels();
    }
    function handleVisibility() {
      if (document.visibilityState === 'visible') loadModels();
    }
    window.addEventListener('focus', handleFocus);
    document.addEventListener('visibilitychange', handleVisibility);
    return () => {
      window.removeEventListener('focus', handleFocus);
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, []);

  const steps: { id: WizardStep; label: string; description: string }[] = [
    {
      id: 'core',
      label: 'Core & Model',
      description: 'Default model, timeouts, storage paths, logging, and the Ollama backend',
    },
    {
      id: 'rag',
      label: 'RAG Configuration',
      description: 'Retrieval-augmented generation and its Cassandra backend',
    },
    {
      id: 'api',
      label: 'API & Auth',
      description: 'API server binding, authentication, and rate limiting',
    },
    {
      id: 'observability',
      label: 'Observability',
      description: 'Tracing, error tracking, monitoring, and log aggregation',
    },
    { id: 'metrics', label: 'Resource Usage', description: 'Monitor system performance and metrics' },
    { id: 'summary', label: 'Summary', description: 'Review and save your configuration' },
  ];

  const currentStepIndex = steps.findIndex((s) => s.id === currentStep);
  const canAdvanceFromCore = formValues.nyxgpt.default_model.trim() !== '';

  function goToNextStep() {
    setCurrentStep(steps[currentStepIndex + 1].id);
  }

  function goToPreviousStep() {
    if (currentStepIndex > 0) {
      setCurrentStep(steps[currentStepIndex - 1].id);
    }
  }

  // Keyboard navigation
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      const target = e.target as HTMLElement;
      if (
        target.tagName === 'INPUT' ||
        target.tagName === 'TEXTAREA' ||
        target.tagName === 'SELECT'
      ) {
        return;
      }

      switch (e.key) {
        case 'ArrowLeft':
          e.preventDefault();
          if (!saving) {
            goToPreviousStep();
          }
          break;
        case 'ArrowRight':
          e.preventDefault();
          if (
            !saving &&
            currentStepIndex < steps.length - 1 &&
            !(currentStep === 'core' && !canAdvanceFromCore)
          ) {
            goToNextStep();
          }
          break;
        case 'Enter':
          e.preventDefault();
          if (currentStep === 'summary' && !saving) {
            handleSave();
          } else if (
            !saving &&
            currentStepIndex < steps.length - 1 &&
            !(currentStep === 'core' && !canAdvanceFromCore)
          ) {
            goToNextStep();
          }
          break;
      }
    }

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentStep, currentStepIndex, canAdvanceFromCore, saving]);

  async function testConnection() {
    setTestingConnection(true);
    setTestResult(null);
    try {
      const res = await fetch('/api/info');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await res.json();
      setTestResult({ success: true, message: 'Connection successful!' });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setTestResult({ success: false, message: `Connection failed: ${msg}` });
    } finally {
      setTestingConnection(false);
    }
  }

  async function handleSave() {
    setSaving(true);
    setSaveError(null);
    setSaveSuccess(false);
    setSaveResult(null);
    setRestartError(null);
    setRestartedTargets([]);
    try {
      const payload = buildSavePayload(formValues);
      const res = await fetch('/api/v1/config/sections', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(extractErrorMessage(data, res.status));
      }

      setSaveSuccess(true);
      setSaveResult(data as SaveResult);
      setSections(data.sections);
      setFormValues(toFormValues(data.sections));
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setSaveError(msg);
    } finally {
      setSaving(false);
    }
  }

  async function handleRestart(targetToRestart: string) {
    setRestarting(targetToRestart);
    setRestartError(null);
    try {
      const res = await fetch('/api/v1/config/restart', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target: targetToRestart }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setRestartedTargets((prev) => [...prev, targetToRestart]);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setRestartError(msg);
    } finally {
      setRestarting(null);
    }
  }

  if (loading) {
    return (
      <main style={{ padding: '2rem', maxWidth: 900, margin: '0 auto' }}>
        <div
          style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 400 }}
        >
          <LoadingSpinner size="large" label="Loading configuration..." />
        </div>
      </main>
    );
  }

  if (configError) {
    return (
      <main style={{ padding: '2rem', maxWidth: 900, margin: '0 auto' }}>
        <h1>Configuration Wizard</h1>
        <ErrorMessage
          title="Failed to load configuration"
          message={configError}
          onRetry={loadSections}
          retrying={loading}
        />
      </main>
    );
  }

  return (
    <main style={{ padding: '2rem', maxWidth: 900, margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ margin: 0, marginBottom: 8 }}>Configuration Wizard</h1>
        <a href="/" style={{ color: '#0066cc', textDecoration: 'none' }}>
          ← Back to Chat
        </a>
      </div>

      {/* Progress Indicator */}
      <div style={{ marginBottom: '2rem' }} role="region" aria-label="Wizard progress">
        <div
          style={{ display: 'flex', gap: 8, marginBottom: 16 }}
          role="progressbar"
          aria-valuenow={currentStepIndex + 1}
          aria-valuemin={1}
          aria-valuemax={steps.length}
          aria-label={`Step ${currentStepIndex + 1} of ${steps.length}`}
        >
          {steps.map((step, idx) => (
            <div
              key={step.id}
              style={{
                flex: 1,
                height: 4,
                background: idx <= currentStepIndex ? '#0066cc' : '#e0e0e0',
                borderRadius: 2,
              }}
              aria-hidden="true"
            />
          ))}
        </div>
        <div style={{ textAlign: 'center' }} aria-live="polite" aria-atomic="true">
          <div style={{ fontWeight: 600, fontSize: 18, marginBottom: 4 }}>
            {steps[currentStepIndex].label}
          </div>
          <div style={{ fontSize: 14, color: '#666' }}>{steps[currentStepIndex].description}</div>
        </div>
      </div>

      {/* Step Content */}
      <div
        style={{
          padding: '2rem',
          border: '1px solid var(--border)',
          borderRadius: 8,
          background: 'var(--background)',
          minHeight: 300,
        }}
        role="region"
        aria-label={`${steps[currentStepIndex].label} configuration`}
      >
        {currentStep === 'core' && (
          <div>
            <h2 style={{ marginTop: 0, fontSize: '1.2rem' }}>Core & Model</h2>
            <p style={{ color: '#666', fontSize: 14, marginBottom: '1.5rem' }}>
              Choose the default model, storage locations, logging, and the Ollama backend.
            </p>

            <div style={{ marginBottom: '1.5rem' }}>
              <label htmlFor="default-model-select" style={fieldLabelStyle}>
                Default Model
              </label>
              <select
                id="default-model-select"
                value={formValues.nyxgpt.default_model}
                onChange={(e) => updateSection('nyxgpt', 'default_model', e.target.value)}
                disabled={loadingModels || saving}
                aria-required="true"
                aria-invalid={!canAdvanceFromCore}
                aria-describedby={
                  loadingModels ? 'models-loading' : modelsError ? 'models-error' : undefined
                }
                style={inputStyle}
              >
                <option value="">Select a model...</option>
                {availableModels.map((model) => (
                  <option key={model} value={model}>
                    {model}
                  </option>
                ))}
              </select>
              {loadingModels && (
                <div
                  id="models-loading"
                  style={{ marginTop: 8, fontSize: 12, color: '#666' }}
                  role="status"
                  aria-live="polite"
                >
                  Loading available models...
                </div>
              )}
              {modelsError && (
                <div
                  id="models-error"
                  style={{
                    marginTop: 8,
                    padding: '10px 12px',
                    borderRadius: 6,
                    fontSize: 12,
                    background: 'var(--error-bg)',
                    color: 'var(--error-text)',
                    border: '1px solid #ffcccc',
                  }}
                  role="alert"
                >
                  ⚠️ Failed to load models: {modelsError}
                </div>
              )}
            </div>

            <TextInput
              id="chat-timeout-seconds"
              label="Chat Timeout (seconds)"
              value={formValues.nyxgpt.chat_timeout_seconds}
              onChange={(v) => updateSection('nyxgpt', 'chat_timeout_seconds', v)}
              disabled={saving}
              type="number"
              hint="How long to wait for a model response before giving up."
            />

            <TextInput
              id="sessions-dir"
              label="Sessions Directory"
              value={formValues.nyxgpt.sessions_dir}
              onChange={(v) => updateSection('nyxgpt', 'sessions_dir', v)}
              disabled={saving}
            />

            <TextInput
              id="vectorstore-dir"
              label="Vectorstore Directory"
              value={formValues.nyxgpt.vectorstore_dir}
              onChange={(v) => updateSection('nyxgpt', 'vectorstore_dir', v)}
              disabled={saving}
            />

            <SelectInput
              id="log-level-select"
              label="Log Level"
              value={formValues.logging.level}
              onChange={(v) => updateSection('logging', 'level', v)}
              disabled={saving}
              options={LOG_LEVEL_OPTIONS}
            />

            <TextInput
              id="log-dir"
              label="Log Directory"
              value={formValues.logging.dir}
              onChange={(v) => updateSection('logging', 'dir', v)}
              disabled={saving}
            />

            <TextInput
              id="ollama-base-url"
              label="Ollama Base URL"
              value={formValues.ollama.base_url}
              onChange={(v) => updateSection('ollama', 'base_url', v)}
              disabled={saving}
              hint="Must start with http:// or https://"
            />

            <div style={{ marginTop: '1.5rem' }}>
              <button
                onClick={testConnection}
                disabled={testingConnection}
                aria-busy={testingConnection}
                aria-describedby={testResult ? 'connection-test-result' : undefined}
                style={{
                  padding: '10px 20px',
                  background: testingConnection ? '#ccc' : '#0066cc',
                  color: 'white',
                  border: 'none',
                  borderRadius: 6,
                  cursor: testingConnection ? 'not-allowed' : 'pointer',
                  fontSize: 14,
                  fontWeight: 600,
                }}
              >
                {testingConnection ? 'Testing...' : 'Test Connection'}
              </button>
              {testResult && (
                <div
                  id="connection-test-result"
                  role={testResult.success ? 'status' : 'alert'}
                  aria-live="polite"
                  style={{
                    marginTop: 10,
                    padding: '10px 12px',
                    borderRadius: 6,
                    fontSize: 14,
                    background: testResult.success ? 'var(--success-bg)' : 'var(--error-bg)',
                    color: testResult.success ? 'var(--success-text)' : 'var(--error-text)',
                    border: `1px solid ${testResult.success ? '#90ee90' : '#ffcccc'}`,
                  }}
                >
                  {testResult.success ? '✓' : '✗'} {testResult.message}
                </div>
              )}
            </div>

            <div
              style={{
                marginTop: '1.5rem',
                padding: '1rem',
                background: 'var(--info-bg)',
                borderRadius: 6,
                fontSize: 14,
              }}
            >
              <strong>💡 Tip:</strong> If you don&apos;t see your model listed, visit the{' '}
              <a href="/models" style={{ color: '#0066cc' }}>
                Models page
              </a>{' '}
              to pull a new model from Ollama.
            </div>
          </div>
        )}

        {currentStep === 'rag' && (
          <div>
            <h2 style={{ marginTop: 0, fontSize: '1.2rem' }}>RAG Configuration</h2>
            <p style={{ color: '#666', fontSize: 14, marginBottom: '1.5rem' }}>
              Enable Retrieval-Augmented Generation to enhance responses with context from your
              documents.
            </p>

            <CheckboxInput
              id="rag-enabled-checkbox"
              label="Enable RAG"
              description="Use vector search to inject relevant context into conversations"
              checked={formValues.rag.enable_chat_context === 'true'}
              onChange={(v) => updateSection('rag', 'enable_chat_context', v ? 'true' : 'false')}
              disabled={saving}
            />

            <TextInput
              id="cassandra-hosts"
              label="Cassandra Hosts"
              value={formValues.rag.cassandra_hosts}
              onChange={(v) => updateSection('rag', 'cassandra_hosts', v)}
              disabled={saving}
              hint="Comma-separated list of hosts, e.g. 127.0.0.1, 10.0.0.2"
            />

            <TextInput
              id="cassandra-port"
              label="Cassandra Port"
              value={formValues.rag.cassandra_port}
              onChange={(v) => updateSection('rag', 'cassandra_port', v)}
              disabled={saving}
              type="number"
            />

            <TextInput
              id="cassandra-keyspace"
              label="Cassandra Keyspace"
              value={formValues.rag.cassandra_keyspace}
              onChange={(v) => updateSection('rag', 'cassandra_keyspace', v)}
              disabled={saving}
            />

            <TextInput
              id="cassandra-table"
              label="Cassandra Table"
              value={formValues.rag.cassandra_table}
              onChange={(v) => updateSection('rag', 'cassandra_table', v)}
              disabled={saving}
            />

            <TextInput
              id="embedding-model"
              label="Embedding Model"
              value={formValues.rag.embedding_model}
              onChange={/* v8 ignore next */ (v) => updateSection('rag', 'embedding_model', v)}
              disabled={saving}
            />

            <div
              style={{ padding: '1rem', background: 'var(--info-bg)', borderRadius: 6, fontSize: 14 }}
            >
              <strong>ℹ️ Note:</strong> RAG requires Apache Cassandra to be running. Changing the
              Cassandra connection or embedding model needs an API restart to take effect --
              offered on the Summary step after saving.
            </div>
          </div>
        )}

        {currentStep === 'api' && (
          <div>
            <h2 style={{ marginTop: 0, fontSize: '1.2rem' }}>API & Auth</h2>
            <p style={{ color: '#666', fontSize: 14, marginBottom: '1.5rem' }}>
              Configure the API server binding, authentication, and rate limiting.
            </p>

            <TextInput
              id="api-host"
              label="API Host"
              value={formValues.api.host}
              onChange={(v) => updateSection('api', 'host', v)}
              disabled={saving}
              hint="Requires an API restart to take effect."
            />

            <TextInput
              id="api-port"
              label="API Port"
              value={formValues.api.port}
              onChange={(v) => updateSection('api', 'port', v)}
              disabled={saving}
              type="number"
              hint="Requires an API restart to take effect."
            />

            <CheckboxInput
              id="auth-enabled-checkbox"
              label="Require API Key"
              description="Protect the API with a shared secret header"
              checked={formValues.auth.enabled === 'true'}
              onChange={(v) => updateSection('auth', 'enabled', v ? 'true' : 'false')}
              disabled={saving}
            />

            <TextInput
              id="auth-header"
              label="Auth Header Name"
              value={formValues.auth.header}
              onChange={(v) => updateSection('auth', 'header', v)}
              disabled={saving}
            />

            <SecretInput
              id="auth-api-key"
              label="API Key"
              value={formValues.auth.api_key}
              onChange={(v) => updateSection('auth', 'api_key', v)}
              disabled={saving}
              set={sections.auth.api_key.set}
              masked={sections.auth.api_key.masked}
              hint="Never displayed in full. Type a new value to rotate it, or leave blank to keep the current key."
            />

            <CheckboxInput
              id="rate-limit-enabled-checkbox"
              label="Enable Rate Limiting"
              description="Throttle requests per client to protect the API"
              checked={formValues.rate_limit.enabled === 'true'}
              onChange={(v) => updateSection('rate_limit', 'enabled', v ? 'true' : 'false')}
              disabled={saving}
            />
          </div>
        )}

        {currentStep === 'observability' && (
          <div>
            <h2 style={{ marginTop: 0, fontSize: '1.2rem' }}>Observability</h2>
            <p style={{ color: '#666', fontSize: 14, marginBottom: '1.5rem' }}>
              Local-only tracing, error tracking, monitoring, and log aggregation. Enabling any of
              these brings up the matching Compose stack automatically when you save.
            </p>

            <CheckboxInput
              id="tracing-enabled-checkbox"
              label="Enable Tracing"
              description="Distributed tracing via a local OTel collector + Jaeger"
              checked={formValues.tracing.enabled === 'true'}
              onChange={(v) => updateSection('tracing', 'enabled', v ? 'true' : 'false')}
              disabled={saving}
            />
            <TextInput
              id="tracing-service-name"
              label="Tracing Service Name"
              value={formValues.tracing.service_name}
              onChange={(v) => updateSection('tracing', 'service_name', v)}
              disabled={saving}
            />
            <TextInput
              id="tracing-otlp-endpoint"
              label="OTLP Endpoint"
              value={formValues.tracing.otlp_endpoint}
              onChange={(v) => updateSection('tracing', 'otlp_endpoint', v)}
              disabled={saving}
              hint="Must start with http:// or https://"
            />

            <CheckboxInput
              id="error-tracking-enabled-checkbox"
              label="Enable Error Tracking"
              description="Report exceptions to a local, self-hosted GlitchTip instance"
              checked={formValues.error_tracking.enabled === 'true'}
              onChange={(v) => updateSection('error_tracking', 'enabled', v ? 'true' : 'false')}
              disabled={saving}
            />
            <SecretInput
              id="error-tracking-dsn"
              label="Error Tracking DSN"
              value={formValues.error_tracking.dsn}
              onChange={(v) => updateSection('error_tracking', 'dsn', v)}
              disabled={saving}
              set={sections.error_tracking.dsn.set}
              masked={sections.error_tracking.dsn.masked}
            />
            <TextInput
              id="error-tracking-environment"
              label="Environment"
              value={formValues.error_tracking.environment}
              onChange={(v) => updateSection('error_tracking', 'environment', v)}
              disabled={saving}
            />

            <CheckboxInput
              id="monitoring-enabled-checkbox"
              label="Enable Monitoring"
              description="Local Grafana + Prometheus dashboards"
              checked={formValues.monitoring.enabled === 'true'}
              onChange={(v) => updateSection('monitoring', 'enabled', v ? 'true' : 'false')}
              disabled={saving}
            />

            <CheckboxInput
              id="log-aggregation-enabled-checkbox"
              label="Enable Log Aggregation"
              description="Ship logs into a local Loki instance, searchable via Grafana Explore"
              checked={formValues.log_aggregation.enabled === 'true'}
              onChange={/* v8 ignore next */ (v) =>
                updateSection('log_aggregation', 'enabled', v ? 'true' : 'false')
              }
              disabled={saving}
            />
          </div>
        )}

        {currentStep === 'metrics' && (
          <div>
            <ResourceMetrics />
          </div>
        )}

        {currentStep === 'summary' && (
          <div>
            <h2 style={{ marginTop: 0, fontSize: '1.2rem' }}>Review Configuration</h2>
            <p style={{ color: '#666', fontSize: 14, marginBottom: '1.5rem' }}>
              Review your settings below and click &quot;Save Configuration&quot; to apply changes.
            </p>

            <div style={{ display: 'grid', gap: '1rem' }}>
              <div
                style={{
                  padding: '1rem',
                  border: '1px solid var(--border)',
                  borderRadius: 6,
                  background: 'var(--background)',
                }}
              >
                <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 8 }}>Core & Model</div>
                <div style={{ fontSize: 14, color: '#666' }}>
                  Default Model: <strong>{formValues.nyxgpt.default_model}</strong>
                </div>
                <div style={{ fontSize: 14, color: '#666' }}>
                  Log Level: <strong>{formValues.logging.level}</strong>
                </div>
                <div style={{ fontSize: 14, color: '#666' }}>
                  Ollama URL: <strong>{formValues.ollama.base_url}</strong>
                </div>
              </div>

              <div
                style={{
                  padding: '1rem',
                  border: '1px solid var(--border)',
                  borderRadius: 6,
                  background: 'var(--background)',
                }}
              >
                <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 8 }}>RAG Configuration</div>
                <div style={{ fontSize: 14, color: '#666' }}>
                  Status: <strong>{formValues.rag.enable_chat_context === 'true' ? 'Enabled' : 'Disabled'}</strong>
                </div>
              </div>

              <div
                style={{
                  padding: '1rem',
                  border: '1px solid var(--border)',
                  borderRadius: 6,
                  background: 'var(--background)',
                }}
              >
                <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 8 }}>API & Auth</div>
                <div style={{ fontSize: 14, color: '#666' }}>
                  Bind: <strong>{formValues.api.host}:{formValues.api.port}</strong>
                </div>
                <div style={{ fontSize: 14, color: '#666' }}>
                  Auth: <strong>{formValues.auth.enabled === 'true' ? 'Required' : 'Disabled'}</strong>
                </div>
              </div>

              <div
                style={{
                  padding: '1rem',
                  border: '1px solid var(--border)',
                  borderRadius: 6,
                  background: 'var(--background)',
                }}
              >
                <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 8 }}>Observability</div>
                <div style={{ fontSize: 14, color: '#666' }}>
                  Tracing: <strong>{formValues.tracing.enabled === 'true' ? 'On' : 'Off'}</strong>
                  {', '}
                  Error tracking:{' '}
                  <strong>{formValues.error_tracking.enabled === 'true' ? 'On' : 'Off'}</strong>
                  {', '}
                  Monitoring: <strong>{formValues.monitoring.enabled === 'true' ? 'On' : 'Off'}</strong>
                  {', '}
                  Log aggregation:{' '}
                  <strong>{formValues.log_aggregation.enabled === 'true' ? 'On' : 'Off'}</strong>
                </div>
              </div>
            </div>

            <div style={{ marginTop: '2rem' }}>
              <button
                onClick={handleSave}
                disabled={saving}
                aria-busy={saving}
                aria-describedby={saveSuccess ? 'save-success' : saveError ? 'save-error' : undefined}
                style={{
                  padding: '12px 24px',
                  background: saving ? '#ccc' : '#28a745',
                  color: 'white',
                  border: 'none',
                  borderRadius: 6,
                  cursor: saving ? 'not-allowed' : 'pointer',
                  fontSize: 16,
                  fontWeight: 600,
                  width: '100%',
                }}
              >
                {saving ? 'Saving...' : 'Save Configuration'}
              </button>

              {saveSuccess && (
                <div
                  id="save-success"
                  role="status"
                  aria-live="polite"
                  style={{
                    marginTop: 12,
                    padding: '12px 16px',
                    borderRadius: 6,
                    fontSize: 14,
                    background: 'var(--success-bg)',
                    color: 'var(--success-text)',
                    border: '1px solid #90ee90',
                  }}
                >
                  <div style={{ fontWeight: 600, marginBottom: 8 }}>
                    ✓ Configuration saved and applied.
                  </div>

                  {saveResult?.observability_reconciled && (
                    <div style={{ fontSize: 13, marginBottom: 8 }}>
                      Observability stack reconciled
                      {saveResult.observability_result?.messages?.length
                        ? `: ${saveResult.observability_result.messages.join('; ')}`
                        : '.'}
                    </div>
                  )}

                  {saveResult && saveResult.restart_required.length > 0 && (
                    <div style={{ fontSize: 13 }}>
                      <div style={{ marginBottom: 6 }}>
                        The following need a restart to fully apply:
                      </div>
                      {saveResult.restart_required.map((target) => (
                        <button
                          key={target}
                          onClick={() => handleRestart(target)}
                          disabled={restarting !== null || restartedTargets.includes(target)}
                          style={{
                            marginRight: 8,
                            marginBottom: 6,
                            padding: '6px 12px',
                            background: restartedTargets.includes(target) ? '#90ee90' : '#0066cc',
                            color: restartedTargets.includes(target) ? '#1a1a1a' : 'white',
                            border: 'none',
                            borderRadius: 6,
                            cursor:
                              restarting !== null || restartedTargets.includes(target)
                                ? 'not-allowed'
                                : 'pointer',
                            fontSize: 13,
                            fontWeight: 600,
                          }}
                        >
                          {restartedTargets.includes(target)
                            ? `${target} restart scheduled`
                            : restarting === target
                              ? 'Scheduling...'
                              : `Restart ${target}`}
                        </button>
                      ))}
                      {restartError && (
                        <div style={{ color: 'var(--error-text)', marginTop: 4 }}>
                          Failed to schedule restart: {restartError}
                        </div>
                      )}
                    </div>
                  )}

                  <div style={{ fontSize: 13, opacity: 0.9, marginTop: 8 }}>
                    <a href="/" style={{ color: 'inherit', textDecoration: 'underline' }}>
                      Return to chat
                    </a>
                    .
                  </div>
                </div>
              )}

              {saveError && (
                <div id="save-error" style={{ marginTop: 12 }}>
                  <ErrorMessage
                    title="Failed to save configuration"
                    message={saveError}
                    onRetry={handleSave}
                    retrying={saving}
                  />
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Navigation */}
      <nav style={{ marginTop: '2rem' }} aria-label="Wizard navigation">
        <div
          style={{ marginBottom: '0.5rem', textAlign: 'center', fontSize: 12, color: '#666' }}
          role="note"
        >
          💡 Use arrow keys (← →) to navigate, Enter to advance or save
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <button
            onClick={goToPreviousStep}
            disabled={currentStepIndex === 0 || saving}
            aria-label={`Go to previous step: ${currentStepIndex > 0 ? steps[currentStepIndex - 1].label : ''}`}
            style={{
              padding: '10px 20px',
              background: currentStepIndex === 0 || saving ? '#ccc' : 'var(--muted)',
              color: currentStepIndex === 0 || saving ? '#999' : 'var(--foreground)',
              border: '1px solid var(--border)',
              borderRadius: 6,
              cursor: currentStepIndex === 0 || saving ? 'not-allowed' : 'pointer',
              fontSize: 14,
              fontWeight: 600,
            }}
          >
            ← Previous
          </button>
          <button
            onClick={goToNextStep}
            disabled={
              currentStepIndex === steps.length - 1 ||
              (currentStep === 'core' && !canAdvanceFromCore) ||
              saving
            }
            aria-label={`Go to next step: ${currentStepIndex < steps.length - 1 ? steps[currentStepIndex + 1].label : ''}`}
            style={{
              padding: '10px 20px',
              background:
                currentStepIndex === steps.length - 1 ||
                (currentStep === 'core' && !canAdvanceFromCore) ||
                saving
                  ? '#ccc'
                  : '#0066cc',
              color: 'white',
              border: 'none',
              borderRadius: 6,
              cursor:
                currentStepIndex === steps.length - 1 ||
                (currentStep === 'core' && !canAdvanceFromCore) ||
                saving
                  ? 'not-allowed'
                  : 'pointer',
              fontSize: 14,
              fontWeight: 600,
            }}
          >
            Next →
          </button>
        </div>
      </nav>
    </main>
  );
}
