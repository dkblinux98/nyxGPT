# Web UI Test Infrastructure

This directory contains the test infrastructure for the nyxGPT web UI (Next.js application).

## Overview

The test infrastructure uses:
- **Vitest** - Fast, modern test runner with native ESM support
- **Happy-DOM** - Lightweight DOM implementation for testing
- **React Testing Library** - Component testing utilities
- **MSW (Mock Service Worker)** - API mocking for tests

## Running Tests

```bash
# Run all tests once and exit
npm test

# Re-run on every save (watch mode -- never terminates)
npm run test:watch

# Run tests with UI
npm run test:ui

# Run all tests once with the 100% coverage gate -- the exact command CI blocks on
npm run test:coverage

# Run a specific test file once
npx vitest run tests/setup.test.ts
```

`npm test` and `npm run test:coverage` terminate: both are `vitest run`. Until
#4209 they were bare `vitest` — watch mode outside CI — so the one documented
way to answer the coverage gate locally never printed a verdict. Watch mode is
`npm run test:watch`, asked for by name.

## Writing Tests

### Basic Test Structure

```typescript
import { describe, it, expect } from 'vitest';

describe('Component Name', () => {
  it('should do something', () => {
    expect(true).toBe(true);
  });
});
```

### Component Testing

Component tests for React/Next.js components run alongside the infrastructure and utility tests. The suite supports:
- ✅ Unit tests for utilities and helpers
- ✅ API mocking with MSW
- ✅ Component tests (React Testing Library + Happy-DOM)

## Files

- `setup.ts` - Global test setup, MSW server initialization
- `mocks/handlers.ts` - MSW request handlers for API endpoints
- `mocks/server.ts` - MSW server configuration
- `setup.test.ts` - Smoke tests to verify infrastructure works
- `README.md` - This file

## API Mocking

API requests are automatically mocked using MSW. Add new mock handlers in `mocks/handlers.ts`:

```typescript
import { http, HttpResponse } from 'msw';

export const handlers = [
  http.get('/api/endpoint', () => {
    return HttpResponse.json({ data: 'mock response' });
  }),
];
```

## Node Version Compatibility

**Current Configuration:**
- Node v20.11.1 (system version)
- happy-dom v20.x (compatible with Node 20.11.1)
- Vitest v4.x
- React Testing Library v16.x

**Note:** Some packages (Vite, @vitejs/plugin-react) show engine warnings but work correctly with Node 20.11.1. Future updates may require Node 20.19.0+.

## Known Limitations

1. **Image Loading** - Next.js Image component may need special handling
2. **Server Components** - React Server Components are not yet fully supported in test environment

## Coverage Gate

`web/vitest.config.ts` enforces a 100% coverage gate (statements/branches/functions/lines). Any regression fails `npm run test:coverage`. Genuinely-untestable files (generated/config) are documented in `coverage.exclude` with a comment rather than lowering the threshold.

The gate is evaluated by `ci-tests.yml`'s `web` job — **on PR heads and on pushes to the release branch** (#4209) — as the same `npm run test:coverage`, and it is a required check (`.github/required-checks.txt`). It was previously `if: github.ref == RELEASE_BRANCH` on a workflow with no `pull_request` trigger, so a web regression was invisible for the whole life of the PR that introduced it and surfaced as a red release branch attributed to whichever change touched `web/` next. `web-gate-smoke.yml` is the executed evidence that the gate now blocks, proven in both directions.

## Future Improvements

- [ ] Add visual regression testing
- [ ] Add E2E testing with Playwright

## Related Documentation

- [Vitest Documentation](https://vitest.dev/)
- [React Testing Library](https://testing-library.com/react)
- [MSW Documentation](https://mswjs.io/)
- [Happy-DOM](https://github.com/capricorn86/happy-dom)
