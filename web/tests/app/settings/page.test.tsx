import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import '@testing-library/jest-dom';
import SettingsPage from '../../../src/app/settings/page';

// Mock useRouter
vi.mock('next/navigation', () => ({
  useRouter: () => ({
    push: vi.fn(),
  }),
}));

// Mock ResourceMetrics component
vi.mock('../../../src/app/settings/ResourceMetrics', () => ({
  default: function MockResourceMetrics() {
    return <div data-testid="resource-metrics">Resource Metrics Component</div>;
  },
}));

// Mock GeneralSettings component
vi.mock('../../../src/app/settings/GeneralSettings', () => ({
  default: function MockGeneralSettings() {
    return <div data-testid="general-settings">General Settings Component</div>;
  },
}));

describe('SettingsPage', () => {
  it('renders settings page with tabs', () => {
    render(<SettingsPage />);

    expect(screen.getByText('Settings')).toBeInTheDocument();
    expect(screen.getByText('Resource Usage')).toBeInTheDocument();
    expect(screen.getByText('General')).toBeInTheDocument();
  });

  it('shows resource usage tab by default', () => {
    render(<SettingsPage />);

    expect(screen.getByTestId('resource-metrics')).toBeInTheDocument();
  });

  it('switches to general tab when clicked', () => {
    render(<SettingsPage />);

    const generalTab = screen.getByRole('button', { name: /General/i });
    fireEvent.click(generalTab);

    expect(screen.getByTestId('general-settings')).toBeInTheDocument();
  });

  it('switches back to resource usage tab', () => {
    render(<SettingsPage />);

    const generalTab = screen.getByRole('button', { name: /General/i });
    fireEvent.click(generalTab);

    const resourceTab = screen.getByRole('button', { name: /Resource Usage/i });
    fireEvent.click(resourceTab);

    expect(screen.getByTestId('resource-metrics')).toBeInTheDocument();
  });

  it('has back button', () => {
    render(<SettingsPage />);

    expect(screen.getByRole('button', { name: /Back/i })).toBeInTheDocument();
  });
});
