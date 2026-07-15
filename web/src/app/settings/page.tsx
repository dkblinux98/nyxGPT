'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import LoadingSpinner from '../../components/LoadingSpinner';
import ErrorMessage from '../../components/ErrorMessage';
import ResourceMetrics from './ResourceMetrics';
import GeneralSettings from './GeneralSettings';

export default function SettingsPage() {
  const router = useRouter();
  const [activeTab, setActiveTab] = useState<'resources' | 'general'>('resources');

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
        <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 16 }}>
          <button
            onClick={() => router.push('/')}
            style={{
              background: 'var(--button)',
              color: 'var(--button-text)',
              border: 'none',
              borderRadius: 8,
              padding: '8px 16px',
              cursor: 'pointer',
              fontSize: 14,
            }}
            onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--button-hover)')}
            onMouseLeave={(e) => (e.currentTarget.style.background = 'var(--button)')}
          >
            ← Back
          </button>
          <h1 style={{ fontSize: 28, fontWeight: 600, margin: 0 }}>Settings</h1>
        </div>

        {/* Tabs */}
        <div style={{ display: 'flex', gap: 8, borderBottom: '2px solid var(--border)' }}>
          <button
            onClick={() => setActiveTab('resources')}
            style={{
              background: activeTab === 'resources' ? 'var(--button)' : 'transparent',
              color: activeTab === 'resources' ? 'var(--button-text)' : 'var(--text)',
              border: 'none',
              borderRadius: '8px 8px 0 0',
              padding: '12px 24px',
              cursor: 'pointer',
              fontSize: 14,
              fontWeight: 500,
              borderBottom: activeTab === 'resources' ? '2px solid var(--primary)' : 'none',
            }}
          >
            Resource Usage
          </button>
          <button
            onClick={() => setActiveTab('general')}
            style={{
              background: activeTab === 'general' ? 'var(--button)' : 'transparent',
              color: activeTab === 'general' ? 'var(--button-text)' : 'var(--text)',
              border: 'none',
              borderRadius: '8px 8px 0 0',
              padding: '12px 24px',
              cursor: 'pointer',
              fontSize: 14,
              fontWeight: 500,
              borderBottom: activeTab === 'general' ? '2px solid var(--primary)' : 'none',
            }}
          >
            General
          </button>
        </div>
      </div>

      {/* Content */}
      <div
        style={{
          maxWidth: 1200,
          margin: '0 auto',
        }}
      >
        {activeTab === 'resources' && <ResourceMetrics />}
        {activeTab === 'general' && <GeneralSettings />}
      </div>
    </div>
  );
}
