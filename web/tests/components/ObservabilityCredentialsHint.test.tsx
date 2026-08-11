import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import ObservabilityCredentialsHint from '../../src/components/ObservabilityCredentialsHint';

// #3718: every SRE surface that links out to Grafana/GlitchTip has to tell the
// operator which wrapped command prints the login. The regression these assert
// against is the one the issue was filed for -- a dashboard that sends you off
// to `ssh` + `cat` a secret file because the API is not allowed to show it.
describe('ObservabilityCredentialsHint', () => {
  it('names the local and cloud wrapped commands', () => {
    render(<ObservabilityCredentialsHint />);

    expect(screen.getByText('nyxgpt ops credentials')).toBeInTheDocument();
    expect(screen.getByText('nyxgpt cloud credentials')).toBeInTheDocument();
  });

  it('mentions both observability UIs by default', () => {
    const { container } = render(<ObservabilityCredentialsHint />);

    expect(container.textContent).toContain('Grafana and GlitchTip admin login');
  });

  it('narrows the subject when a surface only links to one UI', () => {
    const { container } = render(<ObservabilityCredentialsHint services="Grafana" />);

    expect(container.textContent).toContain('Grafana admin login');
    expect(container.textContent).not.toContain('GlitchTip admin login');
  });

  it('never instructs a raw ssh or cat, and says the API will not return them', () => {
    const { container } = render(<ObservabilityCredentialsHint />);
    const text = container.textContent ?? '';

    expect(text).not.toMatch(/\bssh\b/i);
    expect(text).not.toMatch(/\bcat\b/i);
    expect(text).toContain('never returned by the API');
  });
});
