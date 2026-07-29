'use client';

import GeneralSettings from './GeneralSettings';

// The Resource Usage tab (and its ResourceMetrics component) was removed
// and relocated to the System Health screen (#3413) -- that page is now the
// only home for resource metrics. With a single remaining section, this
// page no longer needs tab-switching UI.
export default function SettingsPage() {
  return (
    <div
      style={{
        minHeight: '100vh',
        background: 'var(--bg)',
        color: 'var(--text)',
        padding: 24,
      }}
    >
      {/* Header */}
      <div
        style={{
          maxWidth: 1200,
          margin: '0 auto',
          marginBottom: 24,
        }}
      >
        <div style={{ marginBottom: 16 }}>
          <h1 style={{ fontSize: 28, fontWeight: 600, margin: 0, marginBottom: 8 }}>Settings</h1>
          <a href="/admin/dashboard" style={{ color: '#0066cc', textDecoration: 'none' }}>
            ← Back to Admin Dashboard
          </a>
        </div>
      </div>

      {/* Content */}
      <div
        style={{
          maxWidth: 1200,
          margin: '0 auto',
        }}
      >
        <GeneralSettings />
      </div>
    </div>
  );
}
