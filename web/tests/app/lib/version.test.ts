import { describe, it, expect } from 'vitest';
import { formatVersion } from '@/app/lib/version';

describe('formatVersion (#3716)', () => {
  it('prefixes a bare package version with v', () => {
    expect(formatVersion('3.0.0')).toBe('v3.0.0');
  });

  it('leaves an already-prefixed version alone', () => {
    expect(formatVersion('v3.0.0')).toBe('v3.0.0');
  });

  it('handles pre-release versions', () => {
    expect(formatVersion('3.1.0rc1')).toBe('v3.1.0rc1');
  });

  it('renders nothing when the version is missing', () => {
    expect(formatVersion(null)).toBe('');
    expect(formatVersion(undefined)).toBe('');
    expect(formatVersion('   ')).toBe('');
  });
});
