import { describe, it, expect } from 'vitest';
import {
  channelLabel,
  describeStackVersions,
  formatVersion,
  versionChannel,
} from '@/app/lib/version';

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

/**
 * These mirror `tests/unit/test_version_channel.py::TestVersionChannel` case
 * for case. The two implementations exist because the API classifies its own
 * version in Python while the web tier classifies its own in TypeScript
 * (#3982); if the rules drift, a same-tier stack starts reporting itself as
 * mixed, so the cases are kept identical on purpose.
 */
describe('versionChannel (#3982)', () => {
  it.each(['3.0.0', '3.0', '12.4.1', 'v3.0.0'])('classifies %s as stable', (value) => {
    expect(versionChannel(value)).toBe('stable');
  });

  it.each(['3.0.0rc13', 'v3.0.0rc13', '2.1.0b2', '2.1.0a1'])(
    'classifies %s as a release candidate',
    (value) => {
      expect(versionChannel(value)).toBe('rc');
    },
  );

  it.each(['3.0.0.dev1', '3.0.0+local', 'local', ':local', 'main'])(
    'classifies %s as a working-tree build',
    (value) => {
      expect(versionChannel(value)).toBe('dev');
    },
  );

  it.each([null, undefined, '', '   ', '0.0.0'])('classifies %s as unknown', (value) => {
    expect(versionChannel(value)).toBe('unknown');
  });

  it('never lets a candidate read the same as the release', () => {
    expect(versionChannel('3.0.0rc13')).not.toBe(versionChannel('3.0.0'));
  });

  it('badges only the tiers worth interrupting an operator for', () => {
    // stable is the norm and unknown is the resting state on paths that
    // cannot derive a version -- badging either trains the eye to ignore
    // the badge that matters.
    expect(channelLabel('stable')).toBe('');
    expect(channelLabel('unknown')).toBe('');
    expect(channelLabel('rc')).toBe('rc');
    expect(channelLabel('dev')).toBe('dev');
  });
});

describe('describeStackVersions (#3982)', () => {
  it('flags the mixed stack that shipped past acceptance, naming both versions', () => {
    const stack = describeStackVersions({ apiVersion: '3.0.0', webVersion: '2.1.0' });

    expect(stack.mismatch).toBe(true);
    expect(stack.badge).toBe('mixed');
    expect(stack.detail).toBe('Mixed stack: web v2.1.0 / API v3.0.0');
  });

  it('reports a matched stable stack as neither mixed nor badged', () => {
    const stack = describeStackVersions({ apiVersion: '3.0.0', webVersion: '3.0.0' });

    expect(stack.mismatch).toBe(false);
    expect(stack.badge).toBe('');
  });

  it('keeps the rc suffix and badges the candidate', () => {
    const stack = describeStackVersions({ apiVersion: '3.0.0rc13', webVersion: '3.0.0rc13' });

    expect(stack.apiVersion).toBe('v3.0.0rc13');
    expect(stack.webVersion).toBe('v3.0.0rc13');
    expect(stack.badge).toBe('rc');
    expect(stack.mismatch).toBe(false);
  });

  it('does not call an rc and its own release the same stack', () => {
    const stack = describeStackVersions({ apiVersion: '3.0.0rc13', webVersion: '3.0.0' });

    expect(stack.mismatch).toBe(true);
    expect(stack.detail).toContain('v3.0.0rc13');
  });

  it('treats v-prefixed and bare forms of one build as the same build', () => {
    expect(describeStackVersions({ apiVersion: 'v3.0.0', webVersion: '3.0.0' }).mismatch).toBe(
      false,
    );
  });

  it('never claims a mismatch it cannot establish', () => {
    // Reporting a mixed stack on missing data sends an operator chasing a
    // fault that is not there -- the same misdirection, other direction.
    expect(describeStackVersions({ apiVersion: '3.0.0', webVersion: null }).mismatch).toBe(false);
    expect(describeStackVersions({ apiVersion: null, webVersion: '3.0.0' }).mismatch).toBe(false);
    expect(describeStackVersions({}).mismatch).toBe(false);
  });
});
