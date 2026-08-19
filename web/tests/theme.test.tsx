import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
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

  it('updates the theme when the system preference changes and no user preference is stored', async () => {
    let changeHandler: ((e: MediaQueryListEvent) => void) | undefined;
    const removeEventListener = vi.fn();
    // A real MediaQueryList updates its own `matches` before it dispatches
    // `change`; a mock pinned to one value only works for an implementation
    // that trusts the event payload, and the provider reads the live query
    // instead. Keep the mock's state in sync with the dispatch so this test
    // pins the behavior ("system flips, theme follows") and not the mechanism.
    let systemPrefersDark = false;
    matchMediaMock.mockImplementation(() => ({
      get matches() {
        return systemPrefersDark;
      },
      addEventListener: vi.fn((event: string, handler: (e: MediaQueryListEvent) => void) => {
        if (event === 'change') changeHandler = handler;
      }),
      removeEventListener,
    }));
    const flipSystemPreference = (matches: boolean) => {
      systemPrefersDark = matches;
      changeHandler!({ matches } as MediaQueryListEvent);
    };

    render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('current-theme')).toHaveTextContent('light');
    });

    expect(changeHandler).toBeDefined();
    act(() => {
      flipSystemPreference(true);
    });

    await waitFor(() => {
      expect(screen.getByTestId('current-theme')).toHaveTextContent('dark');
    });

    act(() => {
      flipSystemPreference(false);
    });

    await waitFor(() => {
      expect(screen.getByTestId('current-theme')).toHaveTextContent('light');
    });
  });

  it('ignores system preference changes when the user has an explicit stored preference', async () => {
    localStorage.setItem('theme', 'light');
    let changeHandler: ((e: MediaQueryListEvent) => void) | undefined;
    matchMediaMock.mockReturnValue({
      matches: false,
      addEventListener: vi.fn((event: string, handler: (e: MediaQueryListEvent) => void) => {
        if (event === 'change') changeHandler = handler;
      }),
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

    expect(changeHandler).toBeDefined();
    act(() => {
      changeHandler!({ matches: true } as MediaQueryListEvent);
    });

    // Stored preference wins - theme should remain unchanged
    expect(screen.getByTestId('current-theme')).toHaveTextContent('light');
  });

  it('removes the system theme change listener on unmount', async () => {
    const removeEventListener = vi.fn();
    matchMediaMock.mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener,
    });

    const { unmount } = render(
      <ThemeProvider>
        <TestComponent />
      </ThemeProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('current-theme')).toHaveTextContent('light');
    });

    unmount();
    expect(removeEventListener).toHaveBeenCalledWith('change', expect.any(Function));
  });
});

describe('useTheme outside ThemeProvider', () => {
  it('throws an error when used outside a ThemeProvider', () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    expect(() => {
      render(<TestComponent />);
    }).toThrow('useTheme must be used within a ThemeProvider');

    consoleSpy.mockRestore();
  });
});
