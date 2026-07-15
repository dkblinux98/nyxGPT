import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';

const css = readFileSync(join(__dirname, '../../src/app/globals.css'), 'utf-8');

function rootBlock(selectorStart: string): string {
  const start = css.indexOf(selectorStart);
  const openBrace = css.indexOf('{', start);
  const closeBrace = css.indexOf('}', openBrace);
  return css.slice(openBrace, closeBrace);
}

describe('globals.css compatibility aliases', () => {
  const light = rootBlock(':root {');
  const dark = rootBlock(":root[data-theme='dark']");

  it('defines --background-secondary/--foreground-muted/--border-color used by deploy/canary/self-heal/collections/playground', () => {
    for (const varName of ['--background-secondary', '--foreground-muted', '--border-color']) {
      expect(light).toContain(`${varName}:`);
    }
  });

  it('keeps the light-theme muted-foreground at a WCAG AA-passing shade', () => {
    // #52525b on #f4f4f5 (--muted) is ~7:1; #71717a (~4.4:1) fails AA for small text — do not regress to it.
    const match = light.match(/--muted-foreground:\s*(#[0-9a-fA-F]{6})/);
    expect(match?.[1].toLowerCase()).toBe('#52525b');
  });

  it('defines a theme-aware --link color for both light and dark themes', () => {
    expect(light).toContain('--link: #0066cc');
    expect(dark).toContain('--link: #60a5fa');
  });
});
