'use client';

import { useEffect, useState, type MouseEvent } from 'react';
import LoadingSpinner from '../../components/LoadingSpinner';
import ErrorMessage from '../../components/ErrorMessage';

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

/**
 * One `config.ini` field as declared by the backend's derived schema
 * (`config_wizard.schema_summary()`, #3388). Used to render every option the
 * hand-typed `SectionsData`/`FormValues` shapes above don't yet cover --
 * this is what keeps the wizard from silently truncating `example.config.ini`
 * again the way #3354's original hand-maintained schema did.
 */
interface SchemaField {
  key: string;
  secret: boolean;
  restart_component: string | null;
  observability: boolean;
}

interface SchemaSection {
  section: string;
  label: string;
  fields: SchemaField[];
}

/**
 * Fields already given first-class, hand-built UI elsewhere in this file.
 * Anything in a schema section that ISN'T listed here is rendered generically
 * in the "Additional Settings" step instead -- see `extraFieldsFor`.
 */
const KNOWN_FIELDS: Record<string, string[]> = {
  nyxgpt: ['default_model', 'chat_timeout_seconds', 'sessions_dir', 'vectorstore_dir'],
  logging: ['level', 'dir'],
  ollama: ['base_url'],
  api: ['host', 'port'],
  auth: ['enabled', 'header', 'api_key'],
  rate_limit: ['enabled'],
  rag: [
    'enable_chat_context',
    'cassandra_hosts',
    'cassandra_port',
    'cassandra_keyspace',
    'cassandra_table',
    'embedding_model',
  ],
  tracing: ['enabled', 'service_name', 'otlp_endpoint'],
  error_tracking: ['enabled', 'dsn', 'environment'],
  monitoring: ['enabled'],
  log_aggregation: ['enabled'],
};

/** Groups the generic "Additional Settings" step by topic (owner request, #3388: keep
 * context/pdf/cache grouped with RAG rather than one step per section). */
const EXTRA_GROUPS: { title: string; sections: string[] }[] = [
  { title: 'Core behavior', sections: ['nyxgpt', 'logging', 'ollama', 'prompt', 'batch'] },
  { title: 'API & network', sections: ['api', 'auth', 'rate_limit', 'web'] },
  { title: 'RAG, retrieval & caching', sections: ['rag', 'context', 'pdf', 'cache'] },
  {
    title: 'Observability & self-heal',
    sections: ['tracing', 'error_tracking', 'monitoring', 'log_aggregation', 'self_heal'],
  },
  { title: 'Deployment (Kubernetes)', sections: ['deploy', 'canary'] },
];

function extraFieldsFor(schemaSections: SchemaSection[], section: string): SchemaField[] {
  const known = new Set(KNOWN_FIELDS[section] || []);
  // Every caller passes a `section` sourced from `schemaSections` itself, so
  // the lookup below always finds a match.
  const spec = schemaSections.find((s) => s.section === section)!;
  return spec.fields.filter((f) => !known.has(f.key));
}

function isSecretValue(v: unknown): v is SecretField {
  return typeof v === 'object' && v !== null && 'set' in v;
}

function humanizeKey(key: string): string {
  return key
    .split('_')
    .map((w) => (w.length ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ');
}

/** Infers a widget kind from a field's pristine loaded value (bool/number literals vs. text). */
function inferFieldKind(raw: unknown): 'bool' | 'number' | 'text' {
  if (typeof raw !== 'string') return 'text';
  const v = raw.trim().toLowerCase();
  if (v === 'true' || v === 'false') return 'bool';
  if (v !== '' && !Number.isNaN(Number(v))) return 'number';
  return 'text';
}

/** Recomputes the generic form state's initial values from the raw API response. */
function toExtraValues(
  rawSections: Record<string, Record<string, unknown>>,
  schemaSections: SchemaSection[]
): Record<string, Record<string, string>> {
  const out: Record<string, Record<string, string>> = {};
  for (const spec of schemaSections) {
    const extras = extraFieldsFor(schemaSections, spec.section);
    if (extras.length === 0) continue;
    const values: Record<string, string> = {};
    for (const f of extras) {
      const raw = rawSections[spec.section]?.[f.key];
      values[f.key] = f.secret ? '' : raw != null ? String(raw) : '';
    }
    out[spec.section] = values;
  }
  return out;
}

/**
 * Builds the generic-section portion of the `POST /api/v1/config/sections`
 * payload, mirroring `buildSavePayload`'s "skip untouched defaults" rule.
 */
function buildExtraSavePayload(
  extraValues: Record<string, Record<string, string>>,
  rawSections: Record<string, Record<string, unknown>>,
  fieldDefaults: FieldDefaults,
  schemaSections: SchemaSection[]
): Record<string, Record<string, unknown>> {
  const payload: Record<string, Record<string, unknown>> = {};
  for (const spec of schemaSections) {
    const extras = extraFieldsFor(schemaSections, spec.section);
    for (const f of extras) {
      // toExtraValues seeds an entry for every extra field on load using this
      // same schemaSections/extraFieldsFor computation, so it's always present.
      const current = extraValues[spec.section][f.key];
      if (f.secret) {
        if (current.trim()) {
          if (!payload[spec.section]) payload[spec.section] = {};
          payload[spec.section][f.key] = current.trim();
        }
        continue;
      }
      const raw = rawSections[spec.section]?.[f.key];
      const original = raw != null ? String(raw) : '';
      if (isDefaultField(fieldDefaults, spec.section, f.key) && current === original) continue;
      const kind = inferFieldKind(raw);
      let value: unknown = current.trim();
      if (kind === 'bool') value = current === 'true';
      else if (kind === 'number') value = Number(current);
      if (!payload[spec.section]) payload[spec.section] = {};
      payload[spec.section][f.key] = value;
    }
  }
  return payload;
}

/**
 * Per non-secret field, whether it's currently an inherited default (the key
 * is absent from config.ini and the backend is showing its fallback value)
 * rather than something the user explicitly configured (#3385). Missing
 * section/key entries default to `false` via optional chaining, so mocks
 * and older responses that omit this map behave exactly as before.
 */
type FieldDefaults = Record<string, Record<string, boolean>>;

function isDefaultField(defaults: FieldDefaults, section: string, key: string): boolean {
  return Boolean(defaults[section]?.[key]);
}

type WizardStep = 'core' | 'rag' | 'api' | 'observability' | 'more' | 'summary';

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

/**
 * Builds the `POST /api/v1/config/sections` payload from the wizard's form state.
 *
 * A field left untouched while it was showing an inherited default (per
 * `fieldDefaults`) is omitted rather than sent -- otherwise every save would
 * freeze today's runtime fallback into config.ini as an explicit setting,
 * silently overriding any future default change (#3385). A field the user
 * actually edited is always included, even if the new value happens to
 * match the default.
 */
function buildSavePayload(
  v: FormValues,
  sections: SectionsData,
  fieldDefaults: FieldDefaults
): Record<string, Record<string, unknown>> {
  const payload: Record<string, Record<string, unknown>> = {};

  function include(section: string, key: string, value: unknown, current: string, original: string) {
    if (isDefaultField(fieldDefaults, section, key) && current === original) return;
    if (!payload[section]) payload[section] = {};
    payload[section][key] = value;
  }

  include(
    'nyxgpt',
    'default_model',
    v.nyxgpt.default_model.trim(),
    v.nyxgpt.default_model,
    sections.nyxgpt.default_model
  );
  include(
    'nyxgpt',
    'chat_timeout_seconds',
    Number(v.nyxgpt.chat_timeout_seconds),
    v.nyxgpt.chat_timeout_seconds,
    sections.nyxgpt.chat_timeout_seconds
  );
  include(
    'nyxgpt',
    'sessions_dir',
    v.nyxgpt.sessions_dir.trim(),
    v.nyxgpt.sessions_dir,
    sections.nyxgpt.sessions_dir
  );
  include(
    'nyxgpt',
    'vectorstore_dir',
    v.nyxgpt.vectorstore_dir.trim(),
    v.nyxgpt.vectorstore_dir,
    sections.nyxgpt.vectorstore_dir
  );

  include('logging', 'level', v.logging.level, v.logging.level, sections.logging.level);
  include('logging', 'dir', v.logging.dir.trim(), v.logging.dir, sections.logging.dir);

  include('ollama', 'base_url', v.ollama.base_url.trim(), v.ollama.base_url, sections.ollama.base_url);

  include('api', 'host', v.api.host.trim(), v.api.host, sections.api.host);
  include('api', 'port', Number(v.api.port), v.api.port, sections.api.port);

  include('auth', 'enabled', v.auth.enabled === 'true', v.auth.enabled, sections.auth.enabled);
  include('auth', 'header', v.auth.header.trim(), v.auth.header, sections.auth.header);

  include(
    'rate_limit',
    'enabled',
    v.rate_limit.enabled === 'true',
    v.rate_limit.enabled,
    sections.rate_limit.enabled
  );

  include(
    'rag',
    'enable_chat_context',
    v.rag.enable_chat_context === 'true',
    v.rag.enable_chat_context,
    sections.rag.enable_chat_context
  );
  include(
    'rag',
    'cassandra_hosts',
    v.rag.cassandra_hosts.trim(),
    v.rag.cassandra_hosts,
    sections.rag.cassandra_hosts
  );
  include(
    'rag',
    'cassandra_port',
    Number(v.rag.cassandra_port),
    v.rag.cassandra_port,
    sections.rag.cassandra_port
  );
  include(
    'rag',
    'cassandra_keyspace',
    v.rag.cassandra_keyspace.trim(),
    v.rag.cassandra_keyspace,
    sections.rag.cassandra_keyspace
  );
  include(
    'rag',
    'cassandra_table',
    v.rag.cassandra_table.trim(),
    v.rag.cassandra_table,
    sections.rag.cassandra_table
  );
  include(
    'rag',
    'embedding_model',
    v.rag.embedding_model.trim(),
    v.rag.embedding_model,
    sections.rag.embedding_model
  );

  include(
    'tracing',
    'enabled',
    v.tracing.enabled === 'true',
    v.tracing.enabled,
    sections.tracing.enabled
  );
  include(
    'tracing',
    'service_name',
    v.tracing.service_name.trim(),
    v.tracing.service_name,
    sections.tracing.service_name
  );
  include(
    'tracing',
    'otlp_endpoint',
    v.tracing.otlp_endpoint.trim(),
    v.tracing.otlp_endpoint,
    sections.tracing.otlp_endpoint
  );

  include(
    'error_tracking',
    'enabled',
    v.error_tracking.enabled === 'true',
    v.error_tracking.enabled,
    sections.error_tracking.enabled
  );
  include(
    'error_tracking',
    'environment',
    v.error_tracking.environment.trim(),
    v.error_tracking.environment,
    sections.error_tracking.environment
  );

  include(
    'monitoring',
    'enabled',
    v.monitoring.enabled === 'true',
    v.monitoring.enabled,
    sections.monitoring.enabled
  );
  include(
    'log_aggregation',
    'enabled',
    v.log_aggregation.enabled === 'true',
    v.log_aggregation.enabled,
    sections.log_aggregation.enabled
  );

  if (v.auth.api_key.trim()) {
    if (!payload.auth) payload.auth = {};
    payload.auth.api_key = v.auth.api_key.trim();
  }
  if (v.error_tracking.dsn.trim()) {
    if (!payload.error_tracking) payload.error_tracking = {};
    payload.error_tracking.dsn = v.error_tracking.dsn.trim();
  }
  return payload;
}

/**
 * Merges every non-secret field's current value -- both the hand-built steps'
 * `FormValues` and the schema-derived "Additional Settings" fields -- into one
 * `section.key` map, for the Summary step (#3407). Secret fields are handled
 * separately (`pendingSecretValue`): their cleartext never round-trips into
 * this map, matching the rest of the wizard's masked-secret handling.
 */
function mergedNonSecretValues(
  v: FormValues,
  extra: Record<string, Record<string, string>>
): Record<string, Record<string, unknown>> {
  const out: Record<string, Record<string, unknown>> = {
    nyxgpt: { ...v.nyxgpt },
    logging: { ...v.logging },
    ollama: { ...v.ollama },
    api: { ...v.api },
    auth: { enabled: v.auth.enabled, header: v.auth.header },
    rate_limit: { ...v.rate_limit },
    rag: { ...v.rag },
    tracing: { ...v.tracing },
    error_tracking: { enabled: v.error_tracking.enabled, environment: v.error_tracking.environment },
    monitoring: { ...v.monitoring },
    log_aggregation: { ...v.log_aggregation },
  };
  for (const [section, fields] of Object.entries(extra)) {
    out[section] = { ...out[section], ...fields };
  }
  return out;
}

/**
 * The not-yet-saved value typed into a secret field, whether it's one of the
 * two hand-built secret fields (`auth.api_key`, `error_tracking.dsn`) or a
 * schema-derived "Additional Settings" secret. A non-empty result means
 * saving now would rotate that secret (#3407).
 */
function pendingSecretValue(
  section: string,
  key: string,
  v: FormValues,
  extra: Record<string, Record<string, string>>
): string {
  if (section === 'auth' && key === 'api_key') return v.auth.api_key;
  if (section === 'error_tracking' && key === 'dsn') return v.error_tracking.dsn;
  return extra[section]?.[key] ?? '';
}

/** Formats a Summary-step field value for display: booleans as On/Off, blanks called out. */
function formatSummaryValue(raw: unknown): string {
  if (raw == null) return '(empty)';
  const s = String(raw);
  if (s === 'true') return 'On';
  if (s === 'false') return 'Off';
  return s === '' ? '(empty)' : s;
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

const defaultBadgeStyle = {
  marginLeft: 8,
  fontSize: 11,
  fontWeight: 600,
  color: '#666',
  border: '1px solid var(--border)',
  borderRadius: 4,
  padding: '1px 6px',
  verticalAlign: 'middle',
} as const;

const changedBadgeStyle = {
  marginLeft: 8,
  fontSize: 11,
  fontWeight: 600,
  color: 'var(--link)',
  border: '1px solid var(--link)',
  borderRadius: 4,
  padding: '1px 6px',
  verticalAlign: 'middle',
} as const;

/** Labels a Summary-step value changed this wizard session, vs. what's currently saved (#3407). */
function ChangedBadge({ label = 'changed' }: { label?: string }) {
  return <span style={changedBadgeStyle}>{label}</span>;
}

/** Labels a field's displayed value as an inherited default rather than something explicitly set (#3385). */
function DefaultBadge() {
  return <span style={defaultBadgeStyle}>default</span>;
}

function TextInput({
  id,
  label,
  value,
  onChange,
  disabled,
  hint,
  type = 'text',
  isDefault,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
  hint?: string;
  type?: string;
  isDefault?: boolean;
}) {
  return (
    <div style={{ marginBottom: '1.25rem' }}>
      <div style={{ display: 'flex', alignItems: 'center' }}>
        <label htmlFor={id} style={{ ...fieldLabelStyle, marginBottom: 0 }}>
          {label}
        </label>
        {isDefault && value.trim() !== '' && <DefaultBadge />}
      </div>
      <input
        id={id}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        style={{ ...inputStyle, marginTop: 8 }}
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
  isDefault,
}: {
  id: string;
  label: string;
  description: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled: boolean;
  isDefault?: boolean;
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
          <div style={{ fontWeight: 600, fontSize: 14 }}>
            {label}
            {isDefault && <DefaultBadge />}
          </div>
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
  isDefault,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
  options: { value: string; label: string }[];
  isDefault?: boolean;
}) {
  return (
    <div style={{ marginBottom: '1.25rem' }}>
      <div style={{ display: 'flex', alignItems: 'center' }}>
        <label htmlFor={id} style={{ ...fieldLabelStyle, marginBottom: 0 }}>
          {label}
        </label>
        {isDefault && value.trim() !== '' && <DefaultBadge />}
      </div>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        style={{ ...inputStyle, marginTop: 8 }}
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
  const [fieldDefaults, setFieldDefaults] = useState<FieldDefaults>({});
  const [loading, setLoading] = useState(true);
  const [configError, setConfigError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const [currentStep, setCurrentStep] = useState<WizardStep>('core');
  const [testingConnection, setTestingConnection] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);

  const [formValues, setFormValues] = useState<FormValues>(toFormValues(emptySections()));

  // Generic, schema-driven state for every field not covered by the
  // hand-typed shapes above (#3388) -- see `EXTRA_GROUPS`/`extraFieldsFor`.
  const [schemaSections, setSchemaSections] = useState<SchemaSection[]>([]);
  const [rawSections, setRawSections] = useState<Record<string, Record<string, unknown>>>({});
  const [extraValues, setExtraValues] = useState<Record<string, Record<string, string>>>({});
  const [staleKeys, setStaleKeys] = useState<Record<string, string[]>>({});
  const [removingStaleKey, setRemovingStaleKey] = useState<string | null>(null);

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

  function updateExtra(section: string, key: string, value: string) {
    setExtraValues((prev) => ({
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
      setFieldDefaults(data.field_defaults || {});
      // toFormValues above already requires data.sections to be a populated
      // SectionsData object, so it's guaranteed non-null by this point.
      setFormValues(toFormValues(data.sections));
      const schema: SchemaSection[] = data.schema || [];
      setSchemaSections(schema);
      setRawSections(data.sections);
      setExtraValues(toExtraValues(data.sections, schema));
      setStaleKeys(data.stale_keys || {});
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setConfigError(msg);
    } finally {
      setLoading(false);
    }
  }

  async function handleRemoveStaleKey(section: string, key: string) {
    setRemovingStaleKey(`${section}.${key}`);
    try {
      const res = await fetch('/api/v1/config/sections/stale-keys/remove', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ remove: { [section]: [key] } }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setRawSections(data.sections || {});
      setStaleKeys(data.stale_keys || {});
      setExtraValues(toExtraValues(data.sections || {}, schemaSections));
    } catch {
      // Left in staleKeys -- the user can retry the removal.
    } finally {
      setRemovingStaleKey(null);
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
    {
      id: 'more',
      label: 'Additional Settings',
      description: 'Every other config.ini option, derived directly from example.config.ini',
    },
    { id: 'summary', label: 'Summary', description: 'Review and save your configuration' },
  ];

  const currentStepIndex = steps.findIndex((s) => s.id === currentStep);
  const canAdvanceFromCore = formValues.nyxgpt.default_model.trim() !== '';
  const hasUnsavedChanges =
    JSON.stringify(formValues) !== JSON.stringify(toFormValues(sections)) ||
    JSON.stringify(extraValues) !== JSON.stringify(toExtraValues(rawSections, schemaSections));

  function goToNextStep() {
    setCurrentStep(steps[currentStepIndex + 1].id);
  }

  function goToPreviousStep() {
    if (currentStepIndex > 0) {
      setCurrentStep(steps[currentStepIndex - 1].id);
    }
  }

  /**
   * Cancel discards pending edits and returns to the Admin Dashboard (#3387).
   * With no unsaved changes it exits immediately; otherwise it confirms
   * first since Save applies changes live and can restart services (#3354).
   */
  function handleCancelClick(e: MouseEvent<HTMLAnchorElement>) {
    if (
      hasUnsavedChanges &&
      !confirm('You have unsaved changes. Discard them and return to the Admin Dashboard?')
    ) {
      e.preventDefault();
      return;
    }
    setFormValues(toFormValues(sections));
    setExtraValues(toExtraValues(rawSections, schemaSections));
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

  /** Advances past a successful save: next step, or the Admin Dashboard from the last step (#3407). */
  function advanceAfterSave() {
    if (currentStepIndex === steps.length - 1) {
      window.location.href = '/admin/dashboard';
    } else {
      goToNextStep();
    }
  }

  /**
   * Save persists every pending edit (across all steps, not just the
   * current page) and, on success, advances -- to the next step, or to the
   * Admin Dashboard from the last step (#3407). On failure it stays put
   * with the edits intact so the user can fix and retry; it never
   * navigates on error.
   */
  async function handleSave() {
    setSaving(true);
    setSaveError(null);
    try {
      const payload = buildSavePayload(formValues, sections, fieldDefaults);
      const extraPayload = buildExtraSavePayload(
        extraValues,
        rawSections,
        fieldDefaults,
        schemaSections
      );
      // buildExtraSavePayload only ever adds a section entry alongside a
      // field it's setting, so every value here already has >=1 key.
      for (const [section, fields] of Object.entries(extraPayload)) {
        payload[section] = { ...payload[section], ...fields };
      }
      const hasChanges = Object.values(payload).some((fields) => Object.keys(fields).length > 0);
      if (!hasChanges) {
        // Nothing was touched away from its inherited default -- there's
        // nothing to persist, and posting an empty payload would 400.
        advanceAfterSave();
        return;
      }

      const res = await fetch('/api/v1/config/sections', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(extractErrorMessage(data, res.status));
      }

      setSections(data.sections);
      setFieldDefaults(data.field_defaults || {});
      setFormValues(toFormValues(data.sections));
      setRawSections(data.sections);
      setExtraValues(toExtraValues(data.sections, schemaSections));
      advanceAfterSave();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setSaveError(msg);
    } finally {
      setSaving(false);
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
        <a href="/admin/dashboard" style={{ color: '#0066cc', textDecoration: 'none' }}>
          ← Back to Admin Dashboard
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
              <div style={{ display: 'flex', alignItems: 'center' }}>
                <label htmlFor="default-model-select" style={{ ...fieldLabelStyle, marginBottom: 0 }}>
                  Default Model
                </label>
                {isDefaultField(fieldDefaults, 'nyxgpt', 'default_model') &&
                  formValues.nyxgpt.default_model.trim() !== '' && <DefaultBadge />}
              </div>
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
                style={{ ...inputStyle, marginTop: 8 }}
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
              isDefault={isDefaultField(fieldDefaults, 'nyxgpt', 'chat_timeout_seconds')}
            />

            <TextInput
              id="sessions-dir"
              label="Sessions Directory"
              value={formValues.nyxgpt.sessions_dir}
              onChange={(v) => updateSection('nyxgpt', 'sessions_dir', v)}
              disabled={saving}
              isDefault={isDefaultField(fieldDefaults, 'nyxgpt', 'sessions_dir')}
            />

            <TextInput
              id="vectorstore-dir"
              label="Vectorstore Directory"
              value={formValues.nyxgpt.vectorstore_dir}
              onChange={(v) => updateSection('nyxgpt', 'vectorstore_dir', v)}
              disabled={saving}
              isDefault={isDefaultField(fieldDefaults, 'nyxgpt', 'vectorstore_dir')}
            />

            <SelectInput
              id="log-level-select"
              label="Log Level"
              value={formValues.logging.level}
              onChange={(v) => updateSection('logging', 'level', v)}
              disabled={saving}
              options={LOG_LEVEL_OPTIONS}
              isDefault={isDefaultField(fieldDefaults, 'logging', 'level')}
            />

            <TextInput
              id="log-dir"
              label="Log Directory"
              value={formValues.logging.dir}
              onChange={(v) => updateSection('logging', 'dir', v)}
              disabled={saving}
              isDefault={isDefaultField(fieldDefaults, 'logging', 'dir')}
            />

            <TextInput
              id="ollama-base-url"
              label="Ollama Base URL"
              value={formValues.ollama.base_url}
              onChange={(v) => updateSection('ollama', 'base_url', v)}
              disabled={saving}
              hint="Must start with http:// or https://"
              isDefault={isDefaultField(fieldDefaults, 'ollama', 'base_url')}
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
              isDefault={isDefaultField(fieldDefaults, 'rag', 'enable_chat_context')}
            />

            <TextInput
              id="cassandra-hosts"
              label="Cassandra Hosts"
              value={formValues.rag.cassandra_hosts}
              onChange={(v) => updateSection('rag', 'cassandra_hosts', v)}
              disabled={saving}
              hint="Comma-separated list of hosts, e.g. 127.0.0.1, 10.0.0.2"
              isDefault={isDefaultField(fieldDefaults, 'rag', 'cassandra_hosts')}
            />

            <TextInput
              id="cassandra-port"
              label="Cassandra Port"
              value={formValues.rag.cassandra_port}
              onChange={(v) => updateSection('rag', 'cassandra_port', v)}
              disabled={saving}
              type="number"
              isDefault={isDefaultField(fieldDefaults, 'rag', 'cassandra_port')}
            />

            <TextInput
              id="cassandra-keyspace"
              label="Cassandra Keyspace"
              value={formValues.rag.cassandra_keyspace}
              onChange={(v) => updateSection('rag', 'cassandra_keyspace', v)}
              disabled={saving}
              isDefault={isDefaultField(fieldDefaults, 'rag', 'cassandra_keyspace')}
            />

            <TextInput
              id="cassandra-table"
              label="Cassandra Table"
              value={formValues.rag.cassandra_table}
              onChange={(v) => updateSection('rag', 'cassandra_table', v)}
              disabled={saving}
              isDefault={isDefaultField(fieldDefaults, 'rag', 'cassandra_table')}
            />

            <TextInput
              id="embedding-model"
              label="Embedding Model"
              value={formValues.rag.embedding_model}
              onChange={/* v8 ignore next */ (v) => updateSection('rag', 'embedding_model', v)}
              disabled={saving}
              hint="Optional -- leave blank to use the Default Model above for embeddings."
              isDefault={isDefaultField(fieldDefaults, 'rag', 'embedding_model')}
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
              isDefault={isDefaultField(fieldDefaults, 'api', 'host')}
            />

            <TextInput
              id="api-port"
              label="API Port"
              value={formValues.api.port}
              onChange={(v) => updateSection('api', 'port', v)}
              disabled={saving}
              type="number"
              hint="Requires an API restart to take effect."
              isDefault={isDefaultField(fieldDefaults, 'api', 'port')}
            />

            <CheckboxInput
              id="auth-enabled-checkbox"
              label="Require API Key"
              description="Protect the API with a shared secret header"
              checked={formValues.auth.enabled === 'true'}
              onChange={(v) => updateSection('auth', 'enabled', v ? 'true' : 'false')}
              disabled={saving}
              isDefault={isDefaultField(fieldDefaults, 'auth', 'enabled')}
            />

            <TextInput
              id="auth-header"
              label="Auth Header Name"
              value={formValues.auth.header}
              onChange={(v) => updateSection('auth', 'header', v)}
              disabled={saving}
              isDefault={isDefaultField(fieldDefaults, 'auth', 'header')}
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
              isDefault={isDefaultField(fieldDefaults, 'rate_limit', 'enabled')}
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
              isDefault={isDefaultField(fieldDefaults, 'tracing', 'enabled')}
            />
            <TextInput
              id="tracing-service-name"
              label="Tracing Service Name"
              value={formValues.tracing.service_name}
              onChange={(v) => updateSection('tracing', 'service_name', v)}
              disabled={saving}
              isDefault={isDefaultField(fieldDefaults, 'tracing', 'service_name')}
            />
            <TextInput
              id="tracing-otlp-endpoint"
              label="OTLP Endpoint"
              value={formValues.tracing.otlp_endpoint}
              onChange={(v) => updateSection('tracing', 'otlp_endpoint', v)}
              disabled={saving}
              hint="Must start with http:// or https://"
              isDefault={isDefaultField(fieldDefaults, 'tracing', 'otlp_endpoint')}
            />

            <CheckboxInput
              id="error-tracking-enabled-checkbox"
              label="Enable Error Tracking"
              description="Report exceptions to a local, self-hosted GlitchTip instance"
              checked={formValues.error_tracking.enabled === 'true'}
              onChange={(v) => updateSection('error_tracking', 'enabled', v ? 'true' : 'false')}
              disabled={saving}
              isDefault={isDefaultField(fieldDefaults, 'error_tracking', 'enabled')}
            />
            <SecretInput
              id="error-tracking-dsn"
              label="Error Tracking DSN"
              value={formValues.error_tracking.dsn}
              onChange={(v) => updateSection('error_tracking', 'dsn', v)}
              disabled={saving}
              set={sections.error_tracking.dsn.set}
              masked={sections.error_tracking.dsn.masked}
              hint="Optional -- leave blank to keep error tracking events local-only."
            />
            <TextInput
              id="error-tracking-environment"
              label="Environment"
              value={formValues.error_tracking.environment}
              onChange={(v) => updateSection('error_tracking', 'environment', v)}
              disabled={saving}
              isDefault={isDefaultField(fieldDefaults, 'error_tracking', 'environment')}
            />

            <CheckboxInput
              id="monitoring-enabled-checkbox"
              label="Enable Monitoring"
              description="Local Grafana + Prometheus dashboards"
              checked={formValues.monitoring.enabled === 'true'}
              onChange={(v) => updateSection('monitoring', 'enabled', v ? 'true' : 'false')}
              disabled={saving}
              isDefault={isDefaultField(fieldDefaults, 'monitoring', 'enabled')}
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
              isDefault={isDefaultField(fieldDefaults, 'log_aggregation', 'enabled')}
            />
          </div>
        )}

        {currentStep === 'more' && (
          <div>
            <h2 style={{ marginTop: 0, fontSize: '1.2rem' }}>Additional Settings</h2>
            <p style={{ color: '#666', fontSize: 14, marginBottom: '1.5rem' }}>
              Every other option declared in example.config.ini, grouped by topic and generated
              directly from that file (#3388) -- a new option never goes missing from the wizard.
            </p>

            {Object.keys(staleKeys).length > 0 && (
              <div
                role="alert"
                style={{
                  marginBottom: '1.5rem',
                  padding: '1rem',
                  background: 'var(--error-bg)',
                  color: 'var(--error-text)',
                  border: '1px solid #ffcccc',
                  borderRadius: 6,
                  fontSize: 14,
                }}
              >
                <div style={{ fontWeight: 600, marginBottom: 8 }}>
                  Config.ini has options no longer recognized
                </div>
                {Object.entries(staleKeys).map(([section, keys]) =>
                  keys.map((key) => {
                    const id = `${section}.${key}`;
                    return (
                      <div
                        key={id}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          marginBottom: 6,
                        }}
                      >
                        <code style={{ fontSize: 13 }}>
                          [{section}] {key}
                        </code>
                        <button
                          onClick={() => handleRemoveStaleKey(section, key)}
                          disabled={removingStaleKey !== null}
                          style={{
                            padding: '4px 10px',
                            background: 'transparent',
                            color: 'inherit',
                            border: '1px solid currentColor',
                            borderRadius: 6,
                            cursor: removingStaleKey !== null ? 'not-allowed' : 'pointer',
                            fontSize: 12,
                            fontWeight: 600,
                          }}
                        >
                          {removingStaleKey === id ? 'Removing...' : 'Remove'}
                        </button>
                      </div>
                    );
                  })
                )}
              </div>
            )}

            {EXTRA_GROUPS.map((group) => {
              const groupSections = group.sections
                .map((section) => schemaSections.find((s) => s.section === section))
                .filter((s): s is SchemaSection => Boolean(s))
                .map((spec) => ({ spec, extras: extraFieldsFor(schemaSections, spec.section) }))
                .filter(({ extras }) => extras.length > 0);

              if (groupSections.length === 0) return null;

              return (
                <div key={group.title} style={{ marginBottom: '2rem' }}>
                  <h3 style={{ fontSize: '1rem', marginBottom: '0.75rem' }}>{group.title}</h3>
                  {groupSections.map(({ spec, extras }) => (
                    <div key={spec.section} style={{ marginBottom: '1.5rem' }}>
                      <div
                        style={{
                          fontWeight: 600,
                          fontSize: 12,
                          color: '#666',
                          marginBottom: 8,
                          textTransform: 'uppercase',
                          letterSpacing: 0.5,
                        }}
                      >
                        {spec.label}
                      </div>
                      {extras.map((f) => {
                        const raw = rawSections[spec.section]?.[f.key];
                        // Seeded for every extra field by toExtraValues on load (see buildExtraSavePayload).
                        const value = extraValues[spec.section][f.key];
                        const label = humanizeKey(f.key);
                        const isDefault = isDefaultField(fieldDefaults, spec.section, f.key);
                        const hintParts = [
                          f.restart_component
                            ? `Requires a ${f.restart_component} restart to take effect.`
                            : null,
                          f.observability ? 'Reconciles the observability stack on save.' : null,
                        ].filter(Boolean);
                        const hint = hintParts.length ? hintParts.join(' ') : undefined;
                        const id = `extra-${spec.section}-${f.key}`;

                        if (f.secret) {
                          const secretMeta = isSecretValue(raw) ? raw : EMPTY_SECRET;
                          return (
                            <SecretInput
                              key={f.key}
                              id={id}
                              label={label}
                              value={value}
                              onChange={(v) => updateExtra(spec.section, f.key, v)}
                              disabled={saving}
                              set={secretMeta.set}
                              masked={secretMeta.masked}
                              hint={hint}
                            />
                          );
                        }

                        const kind = inferFieldKind(raw);
                        if (kind === 'bool') {
                          return (
                            <CheckboxInput
                              key={f.key}
                              id={id}
                              label={label}
                              description={hint || ''}
                              checked={value === 'true'}
                              onChange={(v) => updateExtra(spec.section, f.key, v ? 'true' : 'false')}
                              disabled={saving}
                              isDefault={isDefault}
                            />
                          );
                        }

                        return (
                          <TextInput
                            key={f.key}
                            id={id}
                            label={label}
                            value={value}
                            onChange={(v) => updateExtra(spec.section, f.key, v)}
                            disabled={saving}
                            type={kind === 'number' ? 'number' : 'text'}
                            hint={hint}
                            isDefault={isDefault}
                          />
                        );
                      })}
                    </div>
                  ))}
                </div>
              );
            })}
          </div>
        )}

        {currentStep === 'summary' &&
          (() => {
            const currentValues = mergedNonSecretValues(formValues, extraValues);
            const baselineValues = mergedNonSecretValues(
              toFormValues(sections),
              toExtraValues(rawSections, schemaSections)
            );
            return (
              <div>
                <h2 style={{ marginTop: 0, fontSize: '1.2rem' }}>Review Configuration</h2>
                <p style={{ color: '#666', fontSize: 14, marginBottom: '1.5rem' }}>
                  Every section below is derived from the current schema -- a new
                  <code> example.config.ini</code> option appears here automatically. Values changed
                  this session are marked <ChangedBadge />; unchanged inherited defaults are marked{' '}
                  <DefaultBadge />. Secrets are never shown in cleartext.
                </p>

                <div style={{ display: 'grid', gap: '1rem' }} data-testid="summary-sections">
                  {schemaSections.map((spec) => (
                    <div
                      key={spec.section}
                      style={{
                        padding: '1rem',
                        border: '1px solid var(--border)',
                        borderRadius: 6,
                        background: 'var(--background)',
                      }}
                      data-testid={`summary-section-${spec.section}`}
                    >
                      <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 8 }}>
                        {spec.label}
                      </div>
                      {spec.fields.map((f) => {
                        const label = humanizeKey(f.key);
                        if (f.secret) {
                          const secretMeta = isSecretValue(rawSections[spec.section]?.[f.key])
                            ? (rawSections[spec.section][f.key] as SecretField)
                            : EMPTY_SECRET;
                          const willChange = pendingSecretValue(
                            spec.section,
                            f.key,
                            formValues,
                            extraValues
                          ).trim();
                          return (
                            <div
                              key={f.key}
                              style={{ fontSize: 14, color: '#666', marginBottom: 4 }}
                            >
                              {label}:{' '}
                              <strong>{secretMeta.set ? secretMeta.masked : 'Not set'}</strong>
                              {willChange && <ChangedBadge label="will be updated" />}
                            </div>
                          );
                        }
                        const current = currentValues[spec.section]?.[f.key];
                        const baseline = baselineValues[spec.section]?.[f.key];
                        const changed = String(current) !== String(baseline);
                        const isDefault =
                          !changed && isDefaultField(fieldDefaults, spec.section, f.key);
                        return (
                          <div key={f.key} style={{ fontSize: 14, color: '#666', marginBottom: 4 }}>
                            {label}: <strong>{formatSummaryValue(current)}</strong>
                            {changed && <ChangedBadge />}
                            {isDefault && <DefaultBadge />}
                          </div>
                        );
                      })}
                    </div>
                  ))}
                </div>

                <div style={{ fontSize: 13, color: '#666', marginTop: '1rem' }}>
                  Looking for live resource metrics? See{' '}
                  <a href="/settings" style={{ color: '#0066cc' }}>
                    Settings &rarr; Resource Usage
                  </a>
                  . Settings that need a restart to take effect show a restart button on the{' '}
                  <a href="/admin/dashboard" style={{ color: '#0066cc' }}>
                    Admin Dashboard
                  </a>{' '}
                  once saved.
                </div>
              </div>
            );
          })()}

        {saveError && (
          <div id="save-error" style={{ marginTop: '1.5rem' }}>
            <ErrorMessage
              title="Failed to save configuration"
              message={saveError}
              onRetry={handleSave}
              retrying={saving}
            />
          </div>
        )}

        {/*
          Save changes (left) / Cancel (right), attached to the configuration
          box on every step -- not the bottom Previous/Next nav -- per the
          owner's per-page-save design (#3407). Save persists every pending
          edit and advances (next step, or the Admin Dashboard from the last
          step); Cancel discards only edits made since the last save and
          always returns to the Admin Dashboard.
        */}
        <div
          style={{
            marginTop: '2rem',
            paddingTop: '1.5rem',
            borderTop: '1px solid var(--border)',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'flex-start',
            gap: 16,
          }}
        >
          <button
            onClick={handleSave}
            disabled={saving || (currentStep === 'core' && !canAdvanceFromCore)}
            aria-busy={saving}
            aria-describedby={saveError ? 'save-error' : undefined}
            style={{
              padding: '12px 24px',
              background:
                saving || (currentStep === 'core' && !canAdvanceFromCore) ? '#ccc' : '#28a745',
              color: 'white',
              border: 'none',
              borderRadius: 6,
              cursor:
                saving || (currentStep === 'core' && !canAdvanceFromCore)
                  ? 'not-allowed'
                  : 'pointer',
              fontSize: 15,
              fontWeight: 600,
            }}
          >
            {saving
              ? 'Saving...'
              : currentStepIndex === steps.length - 1
                ? 'Save Configuration'
                : 'Save changes'}
          </button>

          <div style={{ textAlign: 'right' }}>
            <a
              href="/admin/dashboard"
              onClick={saving ? (e) => e.preventDefault() : handleCancelClick}
              aria-disabled={saving}
              aria-label="Cancel and return to Admin Dashboard without saving"
              style={{
                padding: '10px 20px',
                background: 'transparent',
                color: saving ? '#999' : 'var(--foreground)',
                border: '1px solid var(--border)',
                borderRadius: 6,
                cursor: saving ? 'not-allowed' : 'pointer',
                fontSize: 14,
                fontWeight: 600,
                textDecoration: 'none',
                display: 'inline-block',
              }}
            >
              Cancel
            </a>
            <div style={{ fontSize: 11, color: '#666', marginTop: 6, maxWidth: 240 }}>
              Discards only edits made since your last save.
            </div>
          </div>
        </div>
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
          <div style={{ display: 'flex', gap: 8 }}>
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
          </div>
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
