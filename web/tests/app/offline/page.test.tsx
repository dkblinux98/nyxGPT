import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import OfflinePage from '../../../src/app/offline/page';

describe('OfflinePage', () => {
  it('renders the offline message', () => {
    render(<OfflinePage />);
    expect(screen.getByText('You are offline')).toBeInTheDocument();
    expect(
      screen.getByText(/lost your connection/i)
    ).toBeInTheDocument();
  });

  it('reloads the page when "Try again" is clicked', () => {
    const reload = vi.fn();
    const originalLocation = window.location;
    // @ts-expect-error - stub window.location.reload for this test
    delete window.location;
    // @ts-expect-error - partial Location stub
    window.location = { ...originalLocation, reload };

    render(<OfflinePage />);
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(reload).toHaveBeenCalledTimes(1);

    window.location = originalLocation;
  });
});
