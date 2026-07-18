import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { ThemeProvider } from '../../src/contexts/ThemeContext';
import ThemeToggle from '../../src/components/ThemeToggle';

// This suite specifically targets the inline event handlers on ThemeToggle
// (onMouseEnter/onMouseLeave/onFocus/onBlur) that mutate style properties
// directly on the DOM node. tests/theme.test.tsx already covers the
// click-to-toggle / label / icon branches, so we focus on the hover and
// focus/blur interaction branches here to close out 100% coverage.
describe('ThemeToggle - hover and focus interactions', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-theme');
    document.documentElement.classList.remove('light', 'dark');
  });

  afterEach(() => {
    localStorage.clear();
  });

  async function renderToggle() {
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
    return screen.getByRole('button');
  }

  it('changes background on mouse enter and reverts on mouse leave', async () => {
    const button = await renderToggle();

    expect(button.style.background).toBe('var(--button-bg)');

    fireEvent.mouseEnter(button);
    expect(button.style.background).toBe('var(--button-hover)');

    fireEvent.mouseLeave(button);
    expect(button.style.background).toBe('var(--button-bg)');
  });

  it('shows a focus outline on focus and removes it on blur', async () => {
    const button = await renderToggle();

    expect(button.style.outline).toBe('2px solid transparent');

    fireEvent.focus(button);
    expect(button.style.outline).toBe('2px solid var(--success)');

    fireEvent.blur(button);
    expect(button.style.outline).toBe('2px solid transparent');
  });

  it('supports keyboard-driven focus/blur and click via user-event', async () => {
    const user = userEvent.setup();
    const button = await renderToggle();

    // Tab into the button - triggers the real focus event handler.
    await user.tab();
    expect(button).toHaveFocus();
    expect(button.style.outline).toBe('2px solid var(--success)');

    // Activate via keyboard, which also blurs-then-refocuses in some
    // environments; assert the toggle behavior regardless.
    await user.keyboard('{Enter}');
    await waitFor(() => {
      expect(button).toHaveTextContent('☀️');
      expect(localStorage.getItem('theme')).toBe('dark');
    });

    // Tab away to blur.
    await user.tab();
    expect(button).not.toHaveFocus();
    expect(button.style.outline).toBe('2px solid transparent');
  });

  it('has expected static styling attributes', async () => {
    const button = await renderToggle();

    expect(button.style.padding).toBe('8px 12px');
    expect(button.style.border).toBe('1px solid var(--border)');
    expect(button.style.borderRadius).toBe('6px');
    expect(button.style.cursor).toBe('pointer');
    expect(button).toHaveAttribute('title', 'Switch to dark mode');
  });
});
