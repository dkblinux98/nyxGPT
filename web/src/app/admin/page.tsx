'use client';

import { useEffect, useState } from 'react';
import LoadingSpinner from '../../components/LoadingSpinner';
import ErrorMessage from '../../components/ErrorMessage';

type ConfigData = {
  ollama_base_url: string;
  default_model: string;
  rag_enabled: boolean;
  log_level: string;
};

type WizardStep = 'model' | 'rag' | 'api' | 'summary';

export default function AdminPage() {
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [loading, setLoading] = useState(true);
  const [configError, setConfigError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);

  // Wizard state
  const [currentStep, setCurrentStep] = useState<WizardStep>('model');
  const [testingConnection, setTestingConnection] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);

  // Form state
  const [formData, setFormData] = useState<ConfigData>({
    ollama_base_url: '',
    default_model: '',
    rag_enabled: false,
    log_level: 'INFO',
  });

  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);

  async function loadConfig() {
    setLoading(true);
    setConfigError(null);
    try {
      const res = await fetch('/api/config');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setConfig(data);
      setFormData({
        ollama_base_url: data.ollama_base_url || '',
        default_model: data.default_model || '',
        rag_enabled: data.rag_enabled || false,
        log_level: data.log_level || 'INFO',
      });
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
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
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
    loadConfig();
    loadModels();
  }, []);

  // Keyboard navigation
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      // Ignore if user is typing in an input field
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
          goToPreviousStep();
          break;
        case 'ArrowRight':
          e.preventDefault();
          // Only advance if validation passes
          if (
            currentStepIndex < steps.length - 1 &&
            !(currentStep === 'model' && !formData.default_model)
          ) {
            goToNextStep();
          }
          break;
        case 'Enter':
          e.preventDefault();
          // On summary page, trigger save if validation passes
          if (currentStep === 'summary' && formData.default_model && !saving) {
            handleSave();
          }
          // Otherwise advance to next step if validation passes
          else if (
            currentStepIndex < steps.length - 1 &&
            !(currentStep === 'model' && !formData.default_model)
          ) {
            goToNextStep();
          }
          break;
      }
    }

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [currentStep, currentStepIndex, formData.default_model, saving]);

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
    try {
      const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          default_model: formData.default_model,
          rag_enabled: formData.rag_enabled,
          log_level: formData.log_level,
        }),
      });

      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || `HTTP ${res.status}`);
      }

      setSaveSuccess(true);
      await loadConfig();
      // Reset wizard to first step after successful save
      setTimeout(() => {
        setSaveSuccess(false);
        setCurrentStep('model');
      }, 3000);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setSaveError(msg);
    } finally {
      setSaving(false);
    }
  }

  const steps: { id: WizardStep; label: string; description: string }[] = [
    { id: 'model', label: 'Model Selection', description: 'Choose your default LLM model' },
    { id: 'rag', label: 'RAG Configuration', description: 'Configure retrieval-augmented generation' },
    { id: 'api', label: 'API Settings', description: 'Configure logging and API settings' },
    { id: 'summary', label: 'Summary', description: 'Review and save your configuration' },
  ];

  const currentStepIndex = steps.findIndex((s) => s.id === currentStep);

  function goToNextStep() {
    if (currentStepIndex < steps.length - 1) {
      setCurrentStep(steps[currentStepIndex + 1].id);
    }
  }

  function goToPreviousStep() {
    if (currentStepIndex > 0) {
      setCurrentStep(steps[currentStepIndex - 1].id);
    }
  }

  if (loading) {
    return (
      <main style={{ padding: '2rem', maxWidth: 900, margin: '0 auto' }}>
        <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 400 }}>
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
          onRetry={loadConfig}
          retrying={loading}
        />
      </main>
    );
  }

  return (
    <main style={{ padding: '2rem', maxWidth: 900, margin: '0 auto' }}>
      <div style={{ marginBottom: '2rem' }}>
        <h1 style={{ margin: 0, marginBottom: 8 }}>Configuration Wizard</h1>
        <a href="/" style={{ color: 'var(--rag-text)', textDecoration: 'none' }}>
          ← Back to Chat
        </a>
      </div>

      {/* Progress Indicator */}
      <div style={{ marginBottom: '2rem' }}>
        <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
          {steps.map((step, idx) => (
            <div
              key={step.id}
              style={{
                flex: 1,
                height: 4,
                background: idx <= currentStepIndex ? 'var(--rag-text)' : 'var(--border)',
                borderRadius: 2,
              }}
            />
          ))}
        </div>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontWeight: 600, fontSize: 18, marginBottom: 4 }}>
            {steps[currentStepIndex].label}
          </div>
          <div style={{ fontSize: 14, color: 'var(--muted-foreground)' }}>
            {steps[currentStepIndex].description}
          </div>
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
      >
        {currentStep === 'model' && (
          <div>
            <h2 style={{ marginTop: 0, fontSize: '1.2rem' }}>Select Default Model</h2>
            <p style={{ color: 'var(--muted-foreground)', fontSize: 14, marginBottom: '1.5rem' }}>
              Choose the LLM model that will be used by default for new conversations.
            </p>

            <div style={{ marginBottom: '1.5rem' }}>
              <label style={{ display: 'block', marginBottom: 8, fontWeight: 600, fontSize: 14 }}>
                Default Model
              </label>
              <select
                value={formData.default_model}
                onChange={(e) => setFormData({ ...formData, default_model: e.target.value })}
                disabled={loadingModels}
                style={{
                  width: '100%',
                  padding: '10px 12px',
                  border: '1px solid var(--border)',
                  borderRadius: 6,
                  fontSize: 14,
                  background: 'var(--background)',
                  color: 'var(--foreground)',
                }}
              >
                <option value="">Select a model...</option>
                {availableModels.map((model) => (
                  <option key={model} value={model}>
                    {model}
                  </option>
                ))}
              </select>
              {loadingModels && (
                <div style={{ marginTop: 8, fontSize: 12, color: 'var(--muted-foreground)' }}>
                  Loading available models...
                </div>
              )}
              {modelsError && (
                <div
                  style={{
                    marginTop: 8,
                    padding: '10px 12px',
                    borderRadius: 6,
                    fontSize: 12,
                    background: 'var(--error-bg)',
                    color: 'var(--error-text)',
                    border: '1px solid var(--error-text)',
                  }}
                >
                  ⚠️ Failed to load models: {modelsError}
                </div>
              )}
            </div>

            <div style={{ padding: '1rem', background: 'var(--info-bg)', borderRadius: 6, fontSize: 14 }}>
              <strong>💡 Tip:</strong> If you don't see your model listed, visit the{' '}
              <a href="/models" style={{ color: 'var(--rag-text)' }}>
                Models page
              </a>{' '}
              to pull a new model from Ollama.
            </div>
          </div>
        )}

        {currentStep === 'rag' && (
          <div>
            <h2 style={{ marginTop: 0, fontSize: '1.2rem' }}>RAG Configuration</h2>
            <p style={{ color: 'var(--muted-foreground)', fontSize: 14, marginBottom: '1.5rem' }}>
              Enable Retrieval-Augmented Generation to enhance responses with context from your documents.
            </p>

            <div style={{ marginBottom: '1.5rem' }}>
              <label
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
                  type="checkbox"
                  checked={formData.rag_enabled}
                  onChange={(e) => setFormData({ ...formData, rag_enabled: e.target.checked })}
                  style={{ width: 18, height: 18, cursor: 'pointer' }}
                />
                <div>
                  <div style={{ fontWeight: 600, fontSize: 14 }}>Enable RAG</div>
                  <div style={{ fontSize: 12, color: 'var(--muted-foreground)', marginTop: 4 }}>
                    Use vector search to inject relevant context into conversations
                  </div>
                </div>
              </label>
            </div>

            <div style={{ padding: '1rem', background: 'var(--info-bg)', borderRadius: 6, fontSize: 14 }}>
              <strong>ℹ️ Note:</strong> RAG requires Apache Cassandra to be running. You can toggle RAG per-session
              in the chat interface or configure advanced settings in{' '}
              <code style={{ background: 'var(--code-bg)', padding: '2px 6px', borderRadius: 4 }}>
                ~/.myGPT/config.ini
              </code>
            </div>
          </div>
        )}

        {currentStep === 'api' && (
          <div>
            <h2 style={{ marginTop: 0, fontSize: '1.2rem' }}>API Settings</h2>
            <p style={{ color: 'var(--muted-foreground)', fontSize: 14, marginBottom: '1.5rem' }}>
              Configure logging level and other API settings.
            </p>

            <div style={{ marginBottom: '1.5rem' }}>
              <label style={{ display: 'block', marginBottom: 8, fontWeight: 600, fontSize: 14 }}>
                Log Level
              </label>
              <select
                value={formData.log_level}
                onChange={(e) => setFormData({ ...formData, log_level: e.target.value })}
                style={{
                  width: '100%',
                  padding: '10px 12px',
                  border: '1px solid var(--border)',
                  borderRadius: 6,
                  fontSize: 14,
                  background: 'var(--background)',
                  color: 'var(--foreground)',
                }}
              >
                <option value="DEBUG">DEBUG - Verbose output for troubleshooting</option>
                <option value="INFO">INFO - Standard operational messages</option>
                <option value="WARNING">WARNING - Warning messages only</option>
                <option value="ERROR">ERROR - Error messages only</option>
              </select>
            </div>

            <div style={{ marginBottom: '1.5rem' }}>
              <label style={{ display: 'block', marginBottom: 8, fontWeight: 600, fontSize: 14 }}>
                Ollama Base URL
              </label>
              <input
                type="text"
                value={formData.ollama_base_url}
                readOnly
                style={{
                  width: '100%',
                  padding: '10px 12px',
                  border: '1px solid var(--border)',
                  borderRadius: 6,
                  fontSize: 14,
                  background: 'var(--muted)',
                  color: 'var(--muted-foreground)',
                  cursor: 'not-allowed',
                }}
              />
              <div style={{ fontSize: 12, color: 'var(--muted-foreground)', marginTop: 4 }}>
                Read-only. Edit in <code>~/.myGPT/config.ini</code> to change.
              </div>
            </div>

            <div style={{ marginTop: '1.5rem' }}>
              <button
                onClick={testConnection}
                disabled={testingConnection}
                style={{
                  padding: '10px 20px',
                  background: testingConnection ? 'var(--muted)' : 'var(--rag-text)',
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
                  style={{
                    marginTop: 10,
                    padding: '10px 12px',
                    borderRadius: 6,
                    fontSize: 14,
                    background: testResult.success ? 'var(--success-bg)' : 'var(--error-bg)',
                    color: testResult.success ? 'var(--success-text)' : 'var(--error-text)',
                    border: `1px solid ${testResult.success ? 'var(--success-text)' : 'var(--error-text)'}`,
                  }}
                >
                  {testResult.success ? '✓' : '✗'} {testResult.message}
                </div>
              )}
            </div>
          </div>
        )}

        {currentStep === 'summary' && (
          <div>
            <h2 style={{ marginTop: 0, fontSize: '1.2rem' }}>Review Configuration</h2>
            <p style={{ color: 'var(--muted-foreground)', fontSize: 14, marginBottom: '1.5rem' }}>
              Review your settings below and click "Save Configuration" to apply changes.
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
                <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 8 }}>Model Selection</div>
                <div style={{ fontSize: 14, color: 'var(--muted-foreground)' }}>
                  Default Model: <strong>{formData.default_model || 'Not set'}</strong>
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
                <div style={{ fontSize: 14, color: 'var(--muted-foreground)' }}>
                  Status: <strong>{formData.rag_enabled ? 'Enabled' : 'Disabled'}</strong>
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
                <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 8 }}>API Settings</div>
                <div style={{ fontSize: 14, color: 'var(--muted-foreground)', marginBottom: 4 }}>
                  Log Level: <strong>{formData.log_level}</strong>
                </div>
                <div style={{ fontSize: 14, color: 'var(--muted-foreground)' }}>
                  Ollama URL: <strong>{formData.ollama_base_url}</strong>
                </div>
              </div>
            </div>

            {!formData.default_model && (
              <div
                style={{
                  marginBottom: '1rem',
                  padding: '12px',
                  borderRadius: 6,
                  fontSize: 14,
                  background: 'var(--error-bg)',
                  color: 'var(--error-text)',
                  border: '1px solid var(--error-text)',
                }}
              >
                ⚠️ <strong>Required:</strong> Please select a default model in step 1 before saving.
              </div>
            )}

            <div style={{ marginTop: '2rem' }}>
              <button
                onClick={handleSave}
                disabled={saving || !formData.default_model}
                style={{
                  padding: '12px 24px',
                  background: saving || !formData.default_model ? 'var(--muted)' : 'var(--success)',
                  color: 'white',
                  border: 'none',
                  borderRadius: 6,
                  cursor: saving || !formData.default_model ? 'not-allowed' : 'pointer',
                  fontSize: 16,
                  fontWeight: 600,
                  width: '100%',
                }}
              >
                {saving ? 'Saving...' : 'Save Configuration'}
              </button>

              {saveSuccess && (
                <div
                  style={{
                    marginTop: 12,
                    padding: '12px 16px',
                    borderRadius: 6,
                    fontSize: 14,
                    background: 'var(--success-bg)',
                    color: 'var(--success-text)',
                    border: '1px solid var(--success-text)',
                  }}
                >
                  <div style={{ fontWeight: 600, marginBottom: 8 }}>
                    ✓ Configuration saved successfully!
                  </div>
                  <div style={{ fontSize: 13, opacity: 0.9 }}>
                    Returning to first step... You can configure again or{' '}
                    <a href="/" style={{ color: 'inherit', textDecoration: 'underline' }}>
                      return to chat
                    </a>
                    .
                  </div>
                </div>
              )}

              {saveError && (
                <div style={{ marginTop: 12 }}>
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
      <div style={{ marginTop: '2rem' }}>
        <div style={{ marginBottom: '0.5rem', textAlign: 'center', fontSize: 12, color: 'var(--muted-foreground)' }}>
          💡 Use arrow keys (← →) to navigate, Enter to advance or save
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between' }}>
          <button
            onClick={goToPreviousStep}
            disabled={currentStepIndex === 0}
            style={{
              padding: '10px 20px',
              background: currentStepIndex === 0 ? 'var(--muted)' : 'var(--button-bg)',
              color: currentStepIndex === 0 ? 'var(--muted-foreground)' : 'var(--foreground)',
              border: '1px solid var(--border)',
              borderRadius: 6,
              cursor: currentStepIndex === 0 ? 'not-allowed' : 'pointer',
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
              (currentStep === 'model' && !formData.default_model)
            }
            style={{
              padding: '10px 20px',
              background:
                currentStepIndex === steps.length - 1 ||
                (currentStep === 'model' && !formData.default_model)
                  ? 'var(--muted)'
                  : 'var(--rag-text)',
              color: 'white',
              border: 'none',
              borderRadius: 6,
              cursor:
                currentStepIndex === steps.length - 1 ||
                (currentStep === 'model' && !formData.default_model)
                  ? 'not-allowed'
                  : 'pointer',
              fontSize: 14,
              fontWeight: 600,
            }}
          >
            Next →
          </button>
        </div>
      </div>
    </main>
  );
}
