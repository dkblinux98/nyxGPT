import { render, screen } from '@testing-library/react';
import LoadingSpinner from '../../src/components/LoadingSpinner';

describe('LoadingSpinner', () => {
  it('renders with default props', () => {
    render(<LoadingSpinner />);
    const spinner = screen.getByRole('status');
    expect(spinner).toBeInTheDocument();
    expect(spinner).toHaveAttribute('aria-label', 'Loading');
  });

  it('renders with custom label', () => {
    render(<LoadingSpinner label="Loading data" />);
    const spinner = screen.getByRole('status');
    expect(spinner).toHaveAttribute('aria-label', 'Loading data');
    expect(screen.getByText('Loading data')).toBeInTheDocument();
  });

  it('renders with small size', () => {
    render(<LoadingSpinner size="small" />);
    const spinner = screen.getByRole('status');
    expect(spinner).toBeInTheDocument();
  });

  it('renders with large size', () => {
    render(<LoadingSpinner size="large" />);
    const spinner = screen.getByRole('status');
    expect(spinner).toBeInTheDocument();
  });
});
