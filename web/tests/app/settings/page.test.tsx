import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import '@testing-library/jest-dom';
import SettingsPage from '../../../src/app/settings/page';

// Mock GeneralSettings component
vi.mock('../../../src/app/settings/GeneralSettings', () => ({
  default: function MockGeneralSettings() {
    return <div data-testid="general-settings">General Settings Component</div>;
  },
}));

describe('SettingsPage', () => {
  it('renders the settings heading and General settings directly, with no tabs', () => {
    render(<SettingsPage />);

    expect(screen.getByText('Settings')).toBeInTheDocument();
    expect(screen.getByTestId('general-settings')).toBeInTheDocument();
    expect(screen.queryByText('Resource Usage')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /General/i })).not.toBeInTheDocument();
  });

  it('has a standard back-to-admin-dashboard link, matching other admin pages', () => {
    render(<SettingsPage />);

    const backLink = screen.getByRole('link', { name: /Back to Admin Dashboard/i });
    expect(backLink).toBeInTheDocument();
    expect(backLink).toHaveAttribute('href', '/admin/dashboard');
  });
});
