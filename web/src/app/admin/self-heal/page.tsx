'use client';

import { useCallback, useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type Component = {
  service: string;
  container: string;
  state: string;
  health: string;
  healthy: boolean;
};

type HealEvent = {
  ts: number;
  service: string;
  reason: string;
  action: string;
  ok: boolean;
  restart_count: number;
  message: string;
};

type SelfHealStatus = {
  enabled: boolean;
  components: Component[];
  unhealthy_count: number;
  events: HealEvent[];
};

export default function SelfHealPage() {
  const router = useRouter();
  const [status, setStatus] = useState<SelfHealStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [toggling, setToggling] = useState(false);
  const [healingService, setHealingService] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    setError(null);
    try {
      const res = await fetch('/api/v1/self-heal/status', { cache: 'no-store' });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      }
      setStatus(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
    const interval = setInterval(loadStatus, 10000);
    return () => clearInterval(interval);
  }, [loadStatus]);

  async function handleToggle() {
    if (!status) return;
    setToggling(true);
    setActionError(null);
    setActionMessage(null);
    try {
      const res = await fetch('/api/v1/self-heal/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !status.enabled }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      }
      setActionMessage(data.enabled ? 'Self-heal enabled' : 'Self-heal disabled');
      await loadStatus();
    } catch (e: unknown) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setToggling(false);
    }
  }

  async function handleHealNow(service?: string) {
    if (service) {
      if (!confirm(`Restart "${service}" now?`)) return;
      setHealingService(service);
    } else {
      if (!confirm('Restart every unhealthy component now?')) return;
      setHealingService('*');
    }
    setActionError(null);
    setActionMessage(null);
    try {
      const res = await fetch('/api/v1/self-heal/heal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(service ? { service } : {}),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      }
      const healed = data.healed as HealEvent[];
      setActionMessage(
        healed.length === 0
          ? 'Nothing to heal (all checked components are healthy)'
          : `Restarted: ${healed.map((h) => h.service).join(', ')}`
      );
      await loadStatus();
    } catch (e: unknown) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setHealingService(null);
    }
  }

  if (loading) {
    return (
      <div style={{ padding: '2rem', textAlign: 'center' }}>
        <LoadingSpinner size="large" />
        <p style={{ marginTop: '1rem' }}>Loading self-heal status...</p>
      </div>
    );
  }

  return (
    <div style={{ padding: '2rem', maxWidth: '900px', margin: '0 auto' }}>
      <div
        style={{
          marginBottom: '2rem',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <div>
          <h1 style={{ fontSize: '2rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>
            Self-Heal
          </h1>
          <p style={{ color: 'var(--foreground-muted)' }}>
            Watches every component of the local Docker Compose stack and automatically restarts
            anything unhealthy or stopped.
          </p>
        </div>
        <button
          onClick={() => router.push('/admin/dashboard')}
          style={{
            padding: '0.5rem 1rem',
            backgroundColor: 'var(--background-secondary)',
            border: '1px solid var(--border-color)',
            borderRadius: '0.375rem',
            cursor: 'pointer',
            fontSize: '0.875rem',
          }}
        >
          Back to Admin Dashboard
        </button>
      </div>

      {error && (
        <div style={{ marginBottom: '1rem' }}>
          <ErrorMessage message={error} onRetry={loadStatus} />
        </div>
      )}

      {actionError && (
        <div style={{ marginBottom: '1rem' }}>
          <ErrorMessage message={actionError} />
        </div>
      )}

      {actionMessage && !actionError && (
        <div
          style={{
            marginBottom: '1rem',
            padding: '0.75rem 1rem',
            borderRadius: '0.375rem',
            background: 'var(--background-secondary)',
            border: '1px solid var(--border-color)',
            fontSize: '0.875rem',
          }}
        >
          {actionMessage}
        </div>
      )}

      {status && (
        <>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '0.75rem',
              marginBottom: '1.5rem',
              flexWrap: 'wrap',
            }}
          >
            <span
              style={{
                fontSize: '0.75rem',
                fontWeight: 600,
                padding: '2px 10px',
                borderRadius: 999,
                background: status.enabled ? '#22c55e' : 'var(--background-secondary)',
                color: status.enabled ? 'white' : 'var(--foreground-muted)',
                border: status.enabled ? 'none' : '1px solid var(--border-color)',
              }}
            >
              {status.enabled ? 'AUTO-HEAL ON' : 'AUTO-HEAL OFF'}
            </span>
            {status.unhealthy_count > 0 && (
              <span
                style={{
                  fontSize: '0.75rem',
                  fontWeight: 600,
                  padding: '2px 10px',
                  borderRadius: 999,
                  background: '#ef4444',
                  color: 'white',
                }}
              >
                {status.unhealthy_count} unhealthy
              </span>
            )}
            <button
              onClick={handleToggle}
              disabled={toggling}
              style={{
                padding: '0.5rem 1rem',
                backgroundColor: status.enabled ? '#ef4444' : '#22c55e',
                color: 'white',
                border: 'none',
                borderRadius: '0.375rem',
                cursor: toggling ? 'not-allowed' : 'pointer',
                fontSize: '0.875rem',
                fontWeight: '600',
              }}
            >
              {toggling
                ? 'Updating...'
                : status.enabled
                  ? 'Disable auto-heal'
                  : 'Enable auto-heal'}
            </button>
            <button
              onClick={() => handleHealNow()}
              disabled={healingService !== null}
              style={{
                padding: '0.5rem 1rem',
                backgroundColor: 'var(--background-secondary)',
                border: '1px solid var(--border-color)',
                borderRadius: '0.375rem',
                cursor: healingService !== null ? 'not-allowed' : 'pointer',
                fontSize: '0.875rem',
                fontWeight: '600',
              }}
            >
              {healingService === '*' ? 'Healing...' : 'Heal all unhealthy now'}
            </button>
            <button
              onClick={loadStatus}
              style={{
                padding: '0.5rem 1rem',
                backgroundColor: 'var(--background-secondary)',
                border: '1px solid var(--border-color)',
                borderRadius: '0.375rem',
                cursor: 'pointer',
                fontSize: '0.875rem',
              }}
            >
              Refresh
            </button>
          </div>

          <div style={{ marginBottom: '1.5rem' }}>
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.75rem' }}>
              Components
            </h2>
            {status.components.length === 0 ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                No Docker Compose containers found. Bring up the stack with{' '}
                <code>docker compose up -d</code> to see live component health here.
              </p>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                {status.components.map((c) => (
                  <div
                    key={c.service}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      padding: '0.75rem 1rem',
                      backgroundColor: 'var(--background-secondary)',
                      borderRadius: '0.5rem',
                      border: '1px solid var(--border-color)',
                    }}
                  >
                    <div>
                      <span style={{ fontWeight: 600 }}>{c.service}</span>
                      <span
                        style={{
                          marginLeft: '0.75rem',
                          fontSize: '0.8rem',
                          color: c.healthy ? '#22c55e' : '#ef4444',
                        }}
                      >
                        {c.healthy ? 'Healthy' : 'Unhealthy'}
                      </span>
                      <span
                        style={{
                          marginLeft: '0.5rem',
                          fontSize: '0.75rem',
                          color: 'var(--foreground-muted)',
                        }}
                      >
                        state={c.state}
                        {c.health ? ` health=${c.health}` : ''}
                      </span>
                    </div>
                    <button
                      onClick={() => handleHealNow(c.service)}
                      disabled={healingService !== null}
                      style={{
                        padding: '0.35rem 0.75rem',
                        backgroundColor: 'var(--background)',
                        border: '1px solid var(--border-color)',
                        borderRadius: '0.375rem',
                        cursor: healingService !== null ? 'not-allowed' : 'pointer',
                        fontSize: '0.8rem',
                      }}
                    >
                      {healingService === c.service ? 'Healing...' : 'Heal now'}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div>
            <h2 style={{ fontSize: '1.1rem', fontWeight: 'bold', marginBottom: '0.75rem' }}>
              Recent heal events
            </h2>
            {status.events.length === 0 ? (
              <p style={{ fontSize: '0.875rem', color: 'var(--foreground-muted)' }}>
                No heal events recorded yet.
              </p>
            ) : (
              <ul
                style={{
                  listStyle: 'none',
                  padding: 0,
                  display: 'flex',
                  flexDirection: 'column',
                  gap: '0.5rem',
                }}
              >
                {[...status.events].reverse().map((event, idx) => (
                  <li
                    key={`${event.ts}-${idx}`}
                    style={{
                      fontSize: '0.875rem',
                      padding: '0.5rem 0.75rem',
                      background: 'var(--background-secondary)',
                      border: '1px solid var(--border-color)',
                      borderRadius: '0.375rem',
                    }}
                  >
                    <span style={{ color: event.ok ? '#22c55e' : '#ef4444', fontWeight: 600 }}>
                      {event.ok ? 'OK' : 'FAILED'}
                    </span>{' '}
                    {event.service}: {event.message} ({event.reason}, restart #
                    {event.restart_count}) at {new Date(event.ts * 1000).toLocaleString()}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </div>
  );
}
