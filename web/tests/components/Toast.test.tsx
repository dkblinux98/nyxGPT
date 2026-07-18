import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Toast, ToastType } from '../../src/components/Toast';

describe('Toast', () => {
  const mockOnDismiss = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    // shouldAdvanceTime keeps real time passing, which userEvent.click()
    // needs internally (its own setTimeout(0) yields never fire otherwise).
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it('renders with success type', () => {
    render(
      <Toast
        id="test-1"
        type="success"
        message="Operation successful"
        onDismiss={mockOnDismiss}
      />
    );

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('Operation successful')).toBeInTheDocument();
    expect(screen.getByText('✓')).toBeInTheDocument();
  });

  it('renders with error type', () => {
    render(
      <Toast
        id="test-2"
        type="error"
        message="Operation failed"
        onDismiss={mockOnDismiss}
      />
    );

    expect(screen.getByText('Operation failed')).toBeInTheDocument();
    expect(screen.getByText('✕')).toBeInTheDocument();
  });

  it('renders with warning type', () => {
    render(
      <Toast
        id="test-3"
        type="warning"
        message="Warning message"
        onDismiss={mockOnDismiss}
      />
    );

    expect(screen.getByText('Warning message')).toBeInTheDocument();
    expect(screen.getByText('⚠')).toBeInTheDocument();
  });

  it('renders with info type', () => {
    render(
      <Toast
        id="test-4"
        type="info"
        message="Info message"
        onDismiss={mockOnDismiss}
      />
    );

    expect(screen.getByText('Info message')).toBeInTheDocument();
    expect(screen.getByText('ℹ')).toBeInTheDocument();
  });

  it('auto-dismisses after specified duration', () => {
    render(
      <Toast
        id="test-5"
        type="success"
        message="Auto dismiss"
        duration={3000}
        onDismiss={mockOnDismiss}
      />
    );

    expect(mockOnDismiss).not.toHaveBeenCalled();

    // Fast-forward time by 3000ms + 200ms for exit animation
    vi.advanceTimersByTime(3200);

    expect(mockOnDismiss).toHaveBeenCalledWith('test-5');
  });

  it('does not auto-dismiss when duration is 0', () => {
    render(
      <Toast
        id="test-6"
        type="success"
        message="No auto dismiss"
        duration={0}
        onDismiss={mockOnDismiss}
      />
    );

    vi.advanceTimersByTime(10000);

    expect(mockOnDismiss).not.toHaveBeenCalled();
  });

  it('renders dismiss button', () => {
    render(
      <Toast
        id="test-7"
        type="success"
        message="Click to dismiss"
        onDismiss={mockOnDismiss}
      />
    );

    const dismissButton = screen.getByLabelText('Dismiss notification');
    expect(dismissButton).toBeInTheDocument();
  });

  it('calls onDismiss when dismiss button is clicked', async () => {
    const user = userEvent.setup({ delay: null });
    render(
      <Toast
        id="test-manual-dismiss"
        type="success"
        message="Manual dismiss test"
        onDismiss={mockOnDismiss}
      />
    );

    const dismissButton = screen.getByLabelText('Dismiss notification');
    await user.click(dismissButton);

    // Wait for exit animation (200ms)
    vi.advanceTimersByTime(200);

    expect(mockOnDismiss).toHaveBeenCalledWith('test-manual-dismiss');
    expect(mockOnDismiss).toHaveBeenCalledTimes(1);
  });

  it('falls back to default icon and background color for an unrecognized type', () => {
    render(
      <Toast
        id="test-unknown"
        type={'unknown' as unknown as ToastType}
        message="Unknown type toast"
        onDismiss={mockOnDismiss}
      />
    );

    // getIcon() default branch returns '' - no known icon glyph renders
    expect(screen.getByText('Unknown type toast')).toBeInTheDocument();
    expect(screen.queryByText('✓')).not.toBeInTheDocument();
    expect(screen.queryByText('✕')).not.toBeInTheDocument();
    expect(screen.queryByText('⚠')).not.toBeInTheDocument();
    expect(screen.queryByText('ℹ')).not.toBeInTheDocument();
  });

  it('restores dismiss button opacity on mouse leave after hovering', () => {
    render(
      <Toast
        id="test-hover"
        type="success"
        message="Hover test"
        onDismiss={mockOnDismiss}
      />
    );

    const dismissButton = screen.getByLabelText('Dismiss notification');

    fireEvent.mouseEnter(dismissButton);
    expect(dismissButton.style.opacity).toBe('1');

    fireEvent.mouseLeave(dismissButton);
    expect(dismissButton.style.opacity).toBe('0.8');
  });

  it('has correct accessibility attributes', () => {
    render(
      <Toast
        id="test-8"
        type="info"
        message="Accessible toast"
        onDismiss={mockOnDismiss}
      />
    );

    const alert = screen.getByRole('alert');
    expect(alert).toHaveAttribute('aria-live', 'assertive');
    expect(alert).toHaveAttribute('aria-atomic', 'true');
  });
});
