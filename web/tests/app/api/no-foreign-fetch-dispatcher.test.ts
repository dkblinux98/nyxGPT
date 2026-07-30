/**
 * Regression test for #3440: a `dispatcher` built from the `undici` version
 * pinned in package.json must never be handed to Next's built-in `fetch`.
 * Next 16 bundles its own (newer-major) undici for global fetch, and a
 * foreign-major Agent's dispatch-handler interface is not guaranteed
 * compatible with it — that mismatch is exactly what made every chat
 * request 502 instantly (`UND_ERR_INVALID_ARG`).
 *
 * chat/stream/route.ts sidesteps the problem by driving the upstream call
 * through undici's own request() instead of fetch(), so it has no reason to
 * use a `dispatcher` option at all. This test enforces that no file under
 * web/src reintroduces a `dispatcher:` option anywhere.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'fs';
import { join } from 'path';

const SRC_DIR = join(process.cwd(), 'src');

function findSourceFiles(dir: string): string[] {
  const entries = readdirSync(dir);
  const files: string[] = [];
  for (const entry of entries) {
    const fullPath = join(dir, entry);
    if (statSync(fullPath).isDirectory()) {
      files.push(...findSourceFiles(fullPath));
    } else if (/\.(ts|tsx)$/.test(entry)) {
      files.push(fullPath);
    }
  }
  return files;
}

describe('no foreign-undici dispatcher is passed into fetch anywhere in web/src', () => {
  const sourceFiles = findSourceFiles(SRC_DIR);

  it('finds source files to check', () => {
    expect(sourceFiles.length).toBeGreaterThan(0);
  });

  it.each(sourceFiles)('%s does not use a `dispatcher:` option', (file) => {
    const source = readFileSync(file, 'utf-8');
    const relativePath = file.replace(process.cwd(), '');

    expect(
      /\bdispatcher\s*:/.test(source),
      `${relativePath} must not pass a \`dispatcher\` option — see #3440`
    ).toBe(false);
  });
});
