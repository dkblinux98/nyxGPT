/**
 * The web tier reporting its *own* version (#3982).
 *
 * The acceptance incident: a 2.1.0 `nyxgpt-web` keg served :3000 against a
 * 3.0.0-line API on :8000, and the header -- which only ever knew the API's
 * version -- read `v3.0.0`. These cases pin the resolution down to the
 * directory layouts the two native installs actually produce, so the number
 * shown for the web tier is the web tier's.
 */
import { mkdtempSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { resolveWebVersion } from '@/lib/webVersion';

describe('resolveWebVersion (#3982)', () => {
  it('prefers an explicitly declared NYXGPT_WEB_VERSION', () => {
    expect(resolveWebVersion({ NYXGPT_WEB_VERSION: '3.0.0rc13' }, '/anywhere')).toEqual({
      version: '3.0.0rc13',
      source: 'env',
    });
  });

  it('lets the declared version override a derivable install path', () => {
    const resolved = resolveWebVersion(
      { NYXGPT_WEB_VERSION: '9.9.9' },
      '/opt/homebrew/Cellar/nyxgpt-web/3.0.0/libexec',
    );

    expect(resolved.version).toBe('9.9.9');
  });

  it('reads the version out of a Homebrew keg path, rc suffix intact', () => {
    expect(
      resolveWebVersion({}, '/opt/homebrew/Cellar/nyxgpt-web/3.0.0rc13/libexec'),
    ).toEqual({ version: '3.0.0rc13', source: 'homebrew-keg' });
  });

  it('reads a versioned rc formula keg (nyxgpt-web@3.0.0rc)', () => {
    // The rc channel stamps `nyxgpt-web@<line>rc` formulas (#3853); the keg
    // directory under it still carries the exact candidate.
    expect(
      resolveWebVersion({}, '/opt/homebrew/Cellar/nyxgpt-web@3.0.0rc/3.0.0rc13/libexec'),
    ).toEqual({ version: '3.0.0rc13', source: 'homebrew-keg' });
  });

  it('reads the Linux native install build directory', () => {
    expect(
      resolveWebVersion({}, '/home/nyx/.nyxGPT/services/nyxgpt-web/build/nyxgpt-web-3.0.0rc13'),
    ).toEqual({ version: '3.0.0rc13', source: 'native-build' });
  });

  it('reports unknown rather than guessing when nothing identifies the build', () => {
    // Notably NOT web/package.json's `0.1.0`: no release path updates it, so
    // reporting it would be a confidently wrong answer -- the exact failure
    // mode this issue is about.
    expect(resolveWebVersion({}, '/Users/dev/nyxGPT/web')).toEqual({
      version: null,
      source: 'unknown',
    });
  });

  it('treats a blank NYXGPT_WEB_VERSION as undeclared', () => {
    expect(resolveWebVersion({ NYXGPT_WEB_VERSION: '   ' }, '/Users/dev/nyxGPT/web').source).toBe(
      'unknown',
    );
  });

  describe('with the process actually standing in the install directory', () => {
    const origin = process.cwd();
    afterEach(() => process.chdir(origin));

    /**
     * The cases above pass path strings in. These change the *real* working
     * directory first, so the defaulted `process.cwd()` argument -- the one
     * the `/api/info` route relies on, and the only reason no build-time
     * plumbing is needed -- is exercised rather than assumed. Both service
     * wrappers `cd` into these shapes before exec'ing the server
     * (`homebrew/nyxgpt-web.rb` does `cd "#{libexec}"`, and
     * `_NATIVE_WEB_WRAPPER_TEMPLATE` in `src/nyxgpt/ops.py` does `cd
     * "<build>/nyxgpt-web-<version>"`), which is what makes the derivation
     * true of a running install and not just of a regex.
     */
    it('derives the version from the real cwd in a keg-shaped tree', () => {
      const root = mkdtempSync(join(tmpdir(), 'nyxgpt-keg-'));
      const keg = join(root, 'Cellar', 'nyxgpt-web', '3.0.0rc13', 'libexec');
      mkdirSync(keg, { recursive: true });
      process.chdir(keg);

      expect(resolveWebVersion({})).toEqual({ version: '3.0.0rc13', source: 'homebrew-keg' });
    });

    it('derives the version from the real cwd in a native build tree', () => {
      const root = mkdtempSync(join(tmpdir(), 'nyxgpt-native-'));
      const build = join(root, 'build', 'nyxgpt-web-3.0.0rc13');
      mkdirSync(build, { recursive: true });
      process.chdir(build);

      expect(resolveWebVersion({})).toEqual({ version: '3.0.0rc13', source: 'native-build' });
    });
  });
});
