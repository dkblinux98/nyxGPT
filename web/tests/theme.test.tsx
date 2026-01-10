import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ThemeProvider, useTheme } from '../src/contexts/ThemeContext';
import ThemeToggle from '../src/components/ThemeToggle';

// Test component that uses the theme hook
function TestComponent() {
  const { theme, setTheme, toggleTheme } = useTheme();
  return (
    <div>
      <div data-testid="current-theme">{theme}</div>
      <button onClick={() => setTheme('light')} data-testid="set-light">
        Set Light
      </button>
      <button onClick={() => setTheme('dark')} data-testid="set-dark">
        Set Dark
      </button>
      <button onClick={toggleTheme} data-testid="toggle">
        Toggle
      </button>
    </div>
  );
}

describe('ThemeContext', () => {
  beforeEach(() => {
    // Clear localStorage before each test
    localStorage.clear();
    // Reset document attributes
    document.documentElement.removeAttribute('data-theme');
    document.documentElement.classList.remove('light', 'dark');
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('provides default light theme', async () => {
    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('current-theme')).toHaveTextContent('light');
    });
  });

  it('allows setting theme to dark', async () => {
    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );

    fireEvent.click(screen.getByTestId('set-dark'));
    await waitFor(() => {
      expect(screen.getByTestId('current-theme')).toHaveTextContent('dark');
    });
  });

  it('allows setting theme to light', async () => {
    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );

    fireEvent.click(screen.getByTestId('set-dark'));
    fireEvent.click(screen.getByTestId('set-light'));
    await waitFor(() => {
      expect(screen.getByTestId('current-theme')).toHaveTextContent('light');
    });
  });

  it('toggles between light and dark themes', async () => {
    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('current-theme')).toHaveTextContent('light');
    });

    fireEvent.click(screen.getByTestId('toggle'));
    await waitFor(() => {
      expect(screen.getByTestId('current-theme')).toHaveTextContent('dark');
    });

    fireEvent.click(screen.getByTestId('toggle'));
    await waitFor(() => {
      expect(screen.getByTestId('current-theme')).toHaveTextContent('light');
    });
  });

  it('persists theme to localStorage', async () => {
    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );

    fireEvent.click(screen.getByTestId('set-dark'));
    await waitFor(() => {
      expect(localStorage.getItem('theme')).toBe('dark');
    });

    fireEvent.click(screen.getByTestId('set-light'));
    await waitFor(() => {
      expect(localStorage.getItem('theme')).toBe('light');
    });
  });

  it('loads theme from localStorage on mount', async () => {
    localStorage.setItem('theme', 'dark');

    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('current-theme')).toHaveTextContent('dark');
    });
  });

  it('applies theme to document', async () => {
    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );

    fireEvent.click(screen.getByTestId('set-dark'));
    await waitFor(() => {
      expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
      expect(document.documentElement.classList.contains('dark')).toBe(true);
    });

    fireEvent.click(screen.getByTestId('set-light'));
    await waitFor(() => {
      expect(document.documentElement.getAttribute('data-theme')).toBe('light');
      expect(document.documentElement.classList.contains('light')).toBe(true);
    });
  });
});

describe('ThemeToggle', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-theme');
    document.documentElement.classList.remove('light', 'dark');
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('renders theme toggle button', async () => {
    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>
    );

    await waitFor(() => {
      const button = screen.getByRole('button');
      expect(button).toBeInTheDocument();
    });
  });

  it('displays moon icon in light mode', async () => {
    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>
    );

    await waitFor(() => {
      const button = screen.getByRole('button');
      expect(button).toHaveTextContent('🌙');
    });
  });

  it('displays sun icon in dark mode', async () => {
    localStorage.setItem('theme', 'dark');

    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>
    );

    await waitFor(() => {
      const button = screen.getByRole('button');
      expect(button).toHaveTextContent('☀️');
    });
  });

  it('toggles theme when clicked', async () => {
    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>
    );

    let button: HTMLElement;
    await waitFor(() => {
      button = screen.getByRole('button');
      expect(button).toHaveTextContent('🌙');
    });

    // Click to switch to dark
    fireEvent.click(button!);
    await waitFor(() => {
      expect(button).toHaveTextContent('☀️');
      expect(localStorage.getItem('theme')).toBe('dark');
    });

    // Click to switch back to light
    fireEvent.click(button!);
    await waitFor(() => {
      expect(button).toHaveTextContent('🌙');
      expect(localStorage.getItem('theme')).toBe('light');
    });
  });

  it('has accessible label', async () => {
    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>
    );

    await waitFor(() => {
      const button = screen.getByRole('button');
      expect(button).toHaveAttribute('aria-label', 'Switch to dark mode');
    });
  });

  it('updates label when theme changes', async () => {
    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>
    );

    let button: HTMLElement;
    await waitFor(() => {
      button = screen.getByRole('button');
      expect(button).toHaveAttribute('aria-label', 'Switch to dark mode');
    });

    fireEvent.click(button!);
    await waitFor(() => {
      expect(button).toHaveAttribute('aria-label', 'Switch to light mode');
    });
  });
});

describe('System Theme Detection', () => {
  let matchMediaMock: vi.Mock;

  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-theme');
    document.documentElement.classList.remove('light', 'dark');

    // Mock matchMedia
    matchMediaMock = vi.fn();
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: matchMediaMock,
    });
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('detects dark system preference', async () => {
    matchMediaMock.mockReturnValue({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    });

    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('current-theme')).toHaveTextContent('dark');
    });
  });

  it('detects light system preference', async () => {
    matchMediaMock.mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    });

    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('current-theme')).toHaveTextContent('light');
    });
  });

  it('prefers localStorage over system preference', async () => {
    localStorage.setItem('theme', 'light');
    matchMediaMock.mockReturnValue({
      matches: true, // System prefers dark
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    });

    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );

    // Should use localStorage value (light) not system preference (dark)
    await waitFor(() => {
      expect(screen.getByTestId('current-theme')).toHaveTextContent('light');
    });
  });
});
