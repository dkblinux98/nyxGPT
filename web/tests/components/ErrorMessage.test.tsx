import { render, screen, fireEvent } from '@testing-library/react';
import { vi } from 'vitest';
import ErrorMessage from '../../src/components/ErrorMessage';

describe('ErrorMessage', () => {
  it('renders error message with default title', () => {
    render(<ErrorMessage message="Something went wrong" />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('Error')).toBeInTheDocument();
    expect(screen.getByText('Something went wrong')).toBeInTheDocument();
  });

  it('renders error message with custom title', () => {
    render(<ErrorMessage message="Network error" title="Connection Failed" />);
    expect(screen.getByText('Connection Failed')).toBeInTheDocument();
    expect(screen.getByText('Network error')).toBeInTheDocument();
  });

  it('renders retry button when onRetry provided', () => {
    const onRetry = vi.fn();
    render(<ErrorMessage message="Failed to load" onRetry={onRetry} />);
    const retryButton = screen.getByRole('button', { name: /retry/i });
    expect(retryButton).toBeInTheDocument();
  });

  it('calls onRetry when retry button clicked', () => {
    const onRetry = vi.fn();
    render(<ErrorMessage message="Failed to load" onRetry={onRetry} />);
    const retryButton = screen.getByRole('button', { name: /retry/i });
    fireEvent.click(retryButton);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('disables retry button when retrying', () => {
    const onRetry = vi.fn();
    render(<ErrorMessage message="Failed to load" onRetry={onRetry} retrying={true} />);
    const retryButton = screen.getByRole('button', { name: /retrying/i });
    expect(retryButton).toBeDisabled();
  });

  it('does not render retry button when onRetry not provided', () => {
    render(<ErrorMessage message="Failed to load" />);
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });
});
