'use client';

import { useEffect, useState, useRef } from 'react';
import LoadingSpinner from '../../../components/LoadingSpinner';
import ErrorMessage from '../../../components/ErrorMessage';

type LogFile = {
  name: string;
  path: string;
  size: number;
  modified: number;
};

type LogLevel = 'ALL' | 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR';

type BackendInfo = {
  ollama_base_url: string;
  default_model: string;
  sessions_dir: string;
};

export default function LogsPage() {
  const [files, setFiles] = useState<LogFile[]>([]);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingContent, setLoadingContent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [contentError, setContentError] = useState<string | null>(null);
  const [backendInfo, setBackendInfo] = useState<BackendInfo | null>(null);

  // Filter states
  const [logLevel, setLogLevel] = useState<LogLevel>('ALL');
  const [searchText, setSearchText] = useState('');
  const [tailLines, setTailLines] = useState<number>(100);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [refreshInterval, setRefreshInterval] = useState<number>(5);

  const logContainerRef = useRef<HTMLDivElement>(null);
  const autoScrollRef = useRef(true);

  // Load log files on mount
  useEffect(() => {
    loadLogFiles();
  }, []);

  // Load backend info on mount
  useEffect(() => {
    async function fetchBackendInfo() {
      try {
        const res = await fetch('/api/info');
        if (res.ok) {
          const data = await res.json();
          setBackendInfo(data);
        }
      } catch (e) {
        console.error('Failed to load backend info:', e);
      }
    }
    fetchBackendInfo();
  }, []);

  // Auto-refresh effect
  useEffect(() => {
    if (!autoRefresh || !selectedFile) return;

    const intervalId = setInterval(() => {
      loadLogContent(selectedFile, false);
    }, refreshInterval * 1000);

    return () => clearInterval(intervalId);
  }, [autoRefresh, selectedFile, refreshInterval, logLevel, searchText, tailLines]);

  async function loadLogFiles() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/logs/files');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setFiles(data.files || []);

      // Auto-select the first file (usually nyxgpt.log)
      if (data.files && data.files.length > 0 && !selectedFile) {
        setSelectedFile(data.files[0].name);
        loadLogContent(data.files[0].name);
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    } finally {
      setLoading(false);
    }
  }

  async function loadLogContent(filename: string, showLoading = true) {
    if (showLoading) {
      setLoadingContent(true);
    }
    setContentError(null);

    try {
      const params = new URLSearchParams();
      if (tailLines > 0) params.set('tail', tailLines.toString());
      if (logLevel !== 'ALL') params.set('level', logLevel);
      if (searchText) params.set('search', searchText);

      const res = await fetch(`/api/v1/logs/view/${filename}?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setLogLines(data.lines || []);

      // Auto-scroll to bottom if enabled
      if (autoScrollRef.current && logContainerRef.current) {
        setTimeout(() => {
          logContainerRef.current?.scrollTo({
            top: logContainerRef.current.scrollHeight,
            behavior: 'smooth',
          });
        }, 100);
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setContentError(msg);
    } finally {
      if (showLoading) {
        setLoadingContent(false);
      }
    }
  }

  function handleFileSelect(filename: string) {
    setSelectedFile(filename);
    loadLogContent(filename);
  }

  function handleDownload() {
    if (!selectedFile) return;
    window.open(`/api/v1/logs/stream/${selectedFile}`, '_blank');
  }

  function handleRefresh() {
    if (selectedFile) {
      loadLogContent(selectedFile);
    }
  }

  function formatBytes(bytes: number): string {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
  }

  function formatDate(timestamp: number): string {
    return new Date(timestamp * 1000).toLocaleString();
  }

  if (loading) {
    return (
      <main style={{ padding: '2rem', maxWidth: 1400, margin: '0 auto' }}>
        <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 400 }}>
          <LoadingSpinner size="large" label="Loading log files..." />
        </div>
      </main>
    );
  }

  if (error) {
    return (
      <main style={{ padding: '2rem', maxWidth: 1400, margin: '0 auto' }}>
        <h1>Log Viewer</h1>
        <ErrorMessage
          title="Failed to load log files"
          message={error}
          onRetry={loadLogFiles}
          retrying={loading}
        />
      </main>
    );
  }

  return (
    <main style={{ padding: '2rem', maxWidth: 1400, margin: '0 auto' }}>
      <div style={{ marginBottom: '1.5rem' }}>
        <h1 style={{ margin: 0, marginBottom: 8 }}>Log Viewer</h1>
        <a href="/" style={{ color: '#0066cc', textDecoration: 'none' }}>
          ← Back to Chat
        </a>
      </div>

      {/* Backend Info */}
      {backendInfo && (
        <div
          style={{
            marginBottom: '1.5rem',
            padding: '1rem',
            border: '1px solid var(--border)',
            borderRadius: 8,
            background: 'var(--background)',
            fontSize: 14,
          }}
        >
          <h2 style={{ margin: 0, marginBottom: '0.75rem', fontSize: '1rem' }}>Backend</h2>
          <ul style={{ margin: 0, paddingLeft: '1.25rem', display: 'flex', flexDirection: 'column', gap: 4 }}>
            <li>
              <strong>Ollama base URL:</strong> {backendInfo.ollama_base_url}
            </li>
            <li>
              <strong>Default model:</strong> {backendInfo.default_model}
            </li>
            <li>
              <strong>Sessions dir:</strong> {backendInfo.sessions_dir}
            </li>
          </ul>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '300px 1fr', gap: '1.5rem' }}>
        {/* Sidebar: File list */}
        <div>
          <div
            style={{
              padding: '1rem',
              border: '1px solid var(--border)',
              borderRadius: 8,
              background: 'var(--background)',
            }}
          >
            <h2 style={{ margin: 0, marginBottom: '1rem', fontSize: '1.1rem' }}>Log Files</h2>
            {files.length === 0 ? (
              <p style={{ color: '#666', fontSize: 14 }}>No log files found</p>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {files.map((file) => (
                  <button
                    key={file.name}
                    onClick={() => handleFileSelect(file.name)}
                    style={{
                      padding: '0.75rem',
                      border: '1px solid var(--border)',
                      borderRadius: 6,
                      background: selectedFile === file.name ? '#0066cc' : 'var(--background)',
                      color: selectedFile === file.name ? 'white' : 'var(--foreground)',
                      cursor: 'pointer',
                      textAlign: 'left',
                      fontSize: 14,
                    }}
                  >
                    <div style={{ fontWeight: 600, marginBottom: 4 }}>{file.name}</div>
                    <div style={{ fontSize: 12, opacity: 0.8 }}>
                      {formatBytes(file.size)} • {formatDate(file.modified)}
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Main: Log content */}
        <div>
          {selectedFile ? (
            <>
              {/* Controls */}
              <div
                style={{
                  padding: '1rem',
                  border: '1px solid var(--border)',
                  borderRadius: 8,
                  background: 'var(--background)',
                  marginBottom: '1rem',
                }}
              >
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '1rem' }}>
                  {/* Log Level Filter */}
                  <div>
                    <label htmlFor="log-level" style={{ display: 'block', marginBottom: 4, fontWeight: 600, fontSize: 14 }}>
                      Log Level
                    </label>
                    <select
                      id="log-level"
                      value={logLevel}
                      onChange={(e) => {
                        setLogLevel(e.target.value as LogLevel);
                        if (selectedFile) loadLogContent(selectedFile);
                      }}
                      style={{
                        width: '100%',
                        padding: '8px 10px',
                        border: '1px solid var(--border)',
                        borderRadius: 6,
                        fontSize: 14,
                        background: 'var(--background)',
                        color: 'var(--foreground)',
                      }}
                    >
                      <option value="ALL">All Levels</option>
                      <option value="DEBUG">DEBUG</option>
                      <option value="INFO">INFO</option>
                      <option value="WARNING">WARNING</option>
                      <option value="ERROR">ERROR</option>
                    </select>
                  </div>

                  {/* Search */}
                  <div>
                    <label htmlFor="search" style={{ display: 'block', marginBottom: 4, fontWeight: 600, fontSize: 14 }}>
                      Search
                    </label>
                    <input
                      id="search"
                      type="text"
                      value={searchText}
                      onChange={(e) => setSearchText(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && selectedFile) {
                          loadLogContent(selectedFile);
                        }
                      }}
                      placeholder="Filter by text..."
                      style={{
                        width: '100%',
                        padding: '8px 10px',
                        border: '1px solid var(--border)',
                        borderRadius: 6,
                        fontSize: 14,
                        background: 'var(--background)',
                        color: 'var(--foreground)',
                      }}
                    />
                  </div>

                  {/* Tail Lines */}
                  <div>
                    <label htmlFor="tail" style={{ display: 'block', marginBottom: 4, fontWeight: 600, fontSize: 14 }}>
                      Tail Lines
                    </label>
                    <input
                      id="tail"
                      type="number"
                      value={tailLines}
                      onChange={(e) => setTailLines(parseInt(e.target.value) || 0)}
                      min="0"
                      placeholder="0 = all"
                      style={{
                        width: '100%',
                        padding: '8px 10px',
                        border: '1px solid var(--border)',
                        borderRadius: 6,
                        fontSize: 14,
                        background: 'var(--background)',
                        color: 'var(--foreground)',
                      }}
                    />
                  </div>
                </div>

                {/* Action buttons */}
                <div style={{ marginTop: '1rem', display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <button
                    onClick={handleRefresh}
                    disabled={loadingContent}
                    style={{
                      padding: '8px 16px',
                      background: '#0066cc',
                      color: 'white',
                      border: 'none',
                      borderRadius: 6,
                      cursor: loadingContent ? 'not-allowed' : 'pointer',
                      fontSize: 14,
                      fontWeight: 600,
                    }}
                  >
                    {loadingContent ? 'Loading...' : '🔄 Refresh'}
                  </button>
                  <button
                    onClick={handleDownload}
                    style={{
                      padding: '8px 16px',
                      background: 'var(--muted)',
                      color: 'var(--foreground)',
                      border: '1px solid var(--border)',
                      borderRadius: 6,
                      cursor: 'pointer',
                      fontSize: 14,
                      fontWeight: 600,
                    }}
                  >
                    💾 Download
                  </button>
                  <label
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      padding: '8px 16px',
                      background: 'var(--muted)',
                      border: '1px solid var(--border)',
                      borderRadius: 6,
                      cursor: 'pointer',
                      fontSize: 14,
                      fontWeight: 600,
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={autoRefresh}
                      onChange={(e) => setAutoRefresh(e.target.checked)}
                      style={{ width: 16, height: 16 }}
                    />
                    Auto-refresh
                  </label>
                  {autoRefresh && (
                    <input
                      type="number"
                      value={refreshInterval}
                      onChange={(e) => setRefreshInterval(parseInt(e.target.value) || 5)}
                      min="1"
                      max="60"
                      placeholder="Seconds"
                      style={{
                        width: 80,
                        padding: '8px 10px',
                        border: '1px solid var(--border)',
                        borderRadius: 6,
                        fontSize: 14,
                        background: 'var(--background)',
                        color: 'var(--foreground)',
                      }}
                    />
                  )}
                </div>
              </div>

              {/* Log content */}
              <div
                style={{
                  border: '1px solid var(--border)',
                  borderRadius: 8,
                  background: '#1e1e1e',
                  overflow: 'hidden',
                }}
              >
                <div
                  style={{
                    padding: '0.75rem 1rem',
                    borderBottom: '1px solid var(--border)',
                    background: '#2d2d2d',
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                  }}
                >
                  <div style={{ fontWeight: 600, fontSize: 14, color: '#fff' }}>
                    {selectedFile} {logLines.length > 0 && `(${logLines.length} lines)`}
                  </div>
                  <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: '#ccc' }}>
                    <input
                      type="checkbox"
                      checked={autoScrollRef.current}
                      onChange={(e) => (autoScrollRef.current = e.target.checked)}
                      style={{ width: 14, height: 14 }}
                    />
                    Auto-scroll
                  </label>
                </div>

                {contentError ? (
                  <div style={{ padding: '2rem' }}>
                    <ErrorMessage
                      title="Failed to load log content"
                      message={contentError}
                      onRetry={handleRefresh}
                      retrying={loadingContent}
                    />
                  </div>
                ) : (
                  <div
                    ref={logContainerRef}
                    style={{
                      padding: '1rem',
                      fontFamily: 'monospace',
                      fontSize: 13,
                      lineHeight: 1.5,
                      color: '#d4d4d4',
                      overflowX: 'auto',
                      overflowY: 'auto',
                      maxHeight: 600,
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-all',
                    }}
                  >
                    {logLines.length === 0 ? (
                      <div style={{ color: '#999', textAlign: 'center', padding: '2rem' }}>
                        No log entries found
                      </div>
                    ) : (
                      logLines.map((line, idx) => (
                        <div
                          key={idx}
                          style={{
                            padding: '2px 0',
                            borderBottom: idx < logLines.length - 1 ? '1px solid #333' : 'none',
                          }}
                        >
                          {line}
                        </div>
                      ))
                    )}
                  </div>
                )}
              </div>
            </>
          ) : (
            <div
              style={{
                padding: '3rem',
                border: '1px solid var(--border)',
                borderRadius: 8,
                background: 'var(--background)',
                textAlign: 'center',
                color: '#666',
              }}
            >
              Select a log file to view its contents
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
