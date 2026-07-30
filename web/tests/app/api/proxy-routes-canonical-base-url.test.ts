/**
 * Regression test for issue #3178: every Next.js API proxy route must reach
 * the backend through the shared apiFetch() helper (web/src/lib/apiProxy.ts)
 * so the base URL resolves from the one canonical env var
 * (NYXGPT_API_BASE_URL) and the X-API-Key auth header is always attached.
 *
 * Prior to this fix, routes drifted onto NEXT_PUBLIC_API_URL (browser-only
 * var, wrong inside the web container) and NYXGPT_API_BASE (a typo missing
 * _URL), and none of the 56 routes attached the auth header at all.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'fs';
import { join } from 'path';

const API_DIR = join(process.cwd(), 'src/app/api');

function findRouteFiles(dir: string): string[] {
  const entries = readdirSync(dir);
  const files: string[] = [];
  for (const entry of entries) {
    const fullPath = join(dir, entry);
    if (statSync(fullPath).isDirectory()) {
      files.push(...findRouteFiles(fullPath));
    } else if (entry === 'route.ts') {
      files.push(fullPath);
    }
  }
  return files;
}

const FORBIDDEN_PATTERNS: Array<{ name: string; pattern: RegExp }> = [
  { name: 'NEXT_PUBLIC_API_URL (browser-only var; unset inside the web container)', pattern: /NEXT_PUBLIC_API_URL/ },
  { name: 'NYXGPT_API_BASE without _URL (typo for NYXGPT_API_BASE_URL)', pattern: /NYXGPT_API_BASE(?!_URL)/ },
  { name: 'hardcoded 127.0.0.1:8000', pattern: /127\.0\.0\.1:8000/ },
  { name: 'hardcoded localhost:8000', pattern: /localhost:8000/ },
];

// #3440: chat/stream drives the upstream call through undici's own
// request() instead of apiFetch()/fetch(), because Next 16's bundled fetch
// rejects a dispatcher built from the pinned (older-major) `undici`
// dependency. It still must resolve the backend URL and attach the auth
// header the same way every other route does — via apiUrl()/attachApiKey()
// from @/lib/apiProxy — so it's checked against an equivalent, not exempted
// from the requirement outright.
const SANCTIONED_NON_APIFETCH_ROUTE = join(API_DIR, 'chat/stream/route.ts');

describe('API proxy routes use the canonical base URL and auth helper', () => {
  const routeFiles = findRouteFiles(API_DIR);

  it('finds proxy routes to check', () => {
    expect(routeFiles.length).toBeGreaterThan(0);
  });

  it.each(routeFiles)('%s uses apiFetch (or the sanctioned equivalent) and no non-canonical base URL', (file) => {
    const source = readFileSync(file, 'utf-8');
    const relativePath = file.replace(process.cwd(), '');

    if (file === SANCTIONED_NON_APIFETCH_ROUTE) {
      expect(source, `${relativePath} must import/use apiUrl() from @/lib/apiProxy`).toMatch(
        /apiUrl\(/
      );
      expect(source, `${relativePath} must import/use attachApiKey() from @/lib/apiProxy`).toMatch(
        /attachApiKey\(/
      );
    } else {
      expect(source, `${relativePath} must import/use apiFetch from @/lib/apiProxy`).toMatch(
        /apiFetch\(/
      );
    }

    for (const { name, pattern } of FORBIDDEN_PATTERNS) {
      expect(
        pattern.test(source),
        `${relativePath} must not reference ${name}`
      ).toBe(false);
    }
  });
});
