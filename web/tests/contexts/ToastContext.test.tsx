import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { ToastProvider, useToast } from '../../src/contexts/ToastContext';

// Test component to use the toast context
function TestComponent() {
  const toast = useToast();

  return (
    <div>
      <button onClick={() => toast.success('Success message')}>Show Success</button>
      <button onClick={() => toast.error('Error message')}>Show Error</button>
      <button onClick={() => toast.warning('Warning message')}>Show Warning</button>
      <button onClick={() => toast.info('Info message')}>Show Info</button>
      <button onClick={() => toast.showToast('success', 'Custom duration', 10)}>
        Show Custom Duration
      </button>
    </div>
  );
}

describe('ToastContext', () => {
  it('throws error when useToast is used outside ToastProvider', () => {
    // Suppress console.error for this test
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation();

    expect(() => {
      render(<TestComponent />);
    }).toThrow('useToast must be used within a ToastProvider');

    consoleSpy.mockRestore();
  });

  it('provides toast methods through context', () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    expect(screen.getByText('Show Success')).toBeInTheDocument();
    expect(screen.getByText('Show Error')).toBeInTheDocument();
    expect(screen.getByText('Show Warning')).toBeInTheDocument();
    expect(screen.getByText('Show Info')).toBeInTheDocument();
  });

  it('renders ToastProvider without crashing', () => {
    const { container } = render(
      <ToastProvider>
        <div>Test content</div>
      </ToastProvider>
    );

    expect(container).toBeInTheDocument();
    expect(screen.getByText('Test content')).toBeInTheDocument();
  });

  it('positions toast container at bottom right', () => {
    const { container } = render(
      <ToastProvider>
        <div>Content</div>
      </ToastProvider>
    );

    const toastContainer = container.querySelector('[style*="position: fixed"]');
    expect(toastContainer).toBeInTheDocument();
  });

  describe('toast lifecycle', () => {
    beforeEach(() => {
      // shouldAdvanceTime keeps real time passing, which userEvent.click()
      // needs internally (its own setTimeout(0) yields never fire otherwise).
      vi.useFakeTimers({ shouldAdvanceTime: true });
    });

    afterEach(() => {
      vi.runOnlyPendingTimers();
      vi.useRealTimers();
    });

    it('shows a success toast when success() is called', async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      await user.click(screen.getByText('Show Success'));

      expect(screen.getByText('Success message')).toBeInTheDocument();
      expect(screen.getByText('✓')).toBeInTheDocument();
    });

    it('shows an error toast when error() is called', async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      await user.click(screen.getByText('Show Error'));

      expect(screen.getByText('Error message')).toBeInTheDocument();
      expect(screen.getByText('✕')).toBeInTheDocument();
    });

    it('shows a warning toast when warning() is called', async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      await user.click(screen.getByText('Show Warning'));

      expect(screen.getByText('Warning message')).toBeInTheDocument();
      expect(screen.getByText('⚠')).toBeInTheDocument();
    });

    it('shows an info toast when info() is called', async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      await user.click(screen.getByText('Show Info'));

      expect(screen.getByText('Info message')).toBeInTheDocument();
      expect(screen.getByText('ℹ')).toBeInTheDocument();
    });

    it('supports a custom duration via showToast()', async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      await user.click(screen.getByText('Show Custom Duration'));
      expect(screen.getByText('Custom duration')).toBeInTheDocument();
    });

    it('dismisses a toast and removes it from the DOM', async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      await user.click(screen.getByText('Show Success'));
      expect(screen.getByText('Success message')).toBeInTheDocument();

      const dismissButton = screen.getByLabelText('Dismiss notification');
      await user.click(dismissButton);

      // Exit animation delay (200ms) before onDismiss actually removes it
      act(() => {
        vi.advanceTimersByTime(200);
      });

      await waitFor(() => {
        expect(screen.queryByText('Success message')).not.toBeInTheDocument();
      });
    });

    it('stacks multiple toasts and dismisses them independently', async () => {
      const user = userEvent.setup({ delay: null });
      render(
        <ToastProvider>
          <TestComponent />
        </ToastProvider>
      );

      await user.click(screen.getByText('Show Success'));
      await user.click(screen.getByText('Show Error'));

      expect(screen.getByText('Success message')).toBeInTheDocument();
      expect(screen.getByText('Error message')).toBeInTheDocument();

      const dismissButtons = screen.getAllByLabelText('Dismiss notification');
      await user.click(dismissButtons[0]);
      vi.advanceTimersByTime(200);

      await waitFor(() => {
        expect(screen.queryByText('Success message')).not.toBeInTheDocument();
      });
      expect(screen.getByText('Error message')).toBeInTheDocument();
    });
  });
});
