'use client';

import { useTheme } from '../contexts/ThemeContext';

export default function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();

  return (
    <button
      onClick={toggleTheme}
      aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
      title={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
      style={{
        padding: '8px 12px',
        background: 'var(--button-bg)',
        border: '1px solid var(--border)',
        borderRadius: 6,
        fontSize: 18,
        cursor: 'pointer',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        outline: '2px solid transparent',
        outlineOffset: 2,
        transition: 'outline 0.2s ease, background 0.2s ease',
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.background = 'var(--button-hover)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.background = 'var(--button-bg)';
      }}
      onFocus={(e) => {
        e.currentTarget.style.outline = '2px solid var(--success)';
      }}
      onBlur={(e) => {
        e.currentTarget.style.outline = '2px solid transparent';
      }}
    >
      {theme === 'light' ? '🌙' : '☀️'}
    </button>
  );
}
