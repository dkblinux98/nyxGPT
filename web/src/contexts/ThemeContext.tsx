'use client';

import { createContext, useContext, useEffect, useSyncExternalStore, ReactNode } from 'react';

type Theme = 'light' | 'dark';

type ThemeContextType = {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
};

const ThemeContext = createContext<ThemeContextType | undefined>(undefined);

// The active theme lives outside React: it is a `localStorage` entry with the
// OS `prefers-color-scheme` as its fallback. `useSyncExternalStore` is the
// sanctioned way to read that -- an effect that reads it and calls setState on
// mount is the `react-hooks/set-state-in-effect` defect, because it renders
// once with the wrong theme and then cascades a second render.
const THEME_EVENT = 'nyxgpt:theme-change';

function subscribeToTheme(onStoreChange: () => void): () => void {
  const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
  mediaQuery.addEventListener('change', onStoreChange);
  // `storage` covers another tab; the custom event covers this one, where
  // `localStorage.setItem` does not fire `storage` for its own window.
  window.addEventListener('storage', onStoreChange);
  window.addEventListener(THEME_EVENT, onStoreChange);
  return () => {
    mediaQuery.removeEventListener('change', onStoreChange);
    window.removeEventListener('storage', onStoreChange);
    window.removeEventListener(THEME_EVENT, onStoreChange);
  };
}

function readTheme(): Theme {
  const stored = localStorage.getItem('theme');
  if (stored === 'light' || stored === 'dark') {
    return stored;
  }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

// The server has neither storage nor a media query, so it renders 'light' --
// the same value the pre-hydration markup carries. A snapshot must be a
// stable value; both of these return a primitive, so React can compare them.
function readServerTheme(): Theme {
  return 'light';
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const theme = useSyncExternalStore(subscribeToTheme, readTheme, readServerTheme);

  // Apply theme to document
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    document.documentElement.classList.remove('light', 'dark');
    document.documentElement.classList.add(theme);
  }, [theme]);

  const setTheme = (newTheme: Theme) => {
    localStorage.setItem('theme', newTheme);
    window.dispatchEvent(new Event(THEME_EVENT));
  };

  const toggleTheme = () => {
    setTheme(theme === 'light' ? 'dark' : 'light');
  };

  return (
    <ThemeContext.Provider value={{ theme, setTheme, toggleTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  const context = useContext(ThemeContext);
  if (context === undefined) {
    throw new Error('useTheme must be used within a ThemeProvider');
  }
  return context;
}
