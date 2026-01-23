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
# Run all tests
npm test

# Run tests with UI
npm run test:ui

# Run tests with coverage
npm run test:coverage

# Run specific test file
npx vitest tests/setup.test.ts
```

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

### Component Testing (Coming Soon)

Component testing for React/Next.js components requires additional configuration and is planned for future iterations. The current infrastructure supports:
- ✅ Unit tests for utilities and helpers
- ✅ API mocking with MSW
- ⚠️ Component tests (requires Next.js runtime mocking - see #2776)

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

1. **Next.js Component Testing** - Full component tests for Next.js components require additional runtime mocking (issue #2776)
2. **Image Loading** - Next.js Image component may need special handling
3. **Server Components** - React Server Components are not yet fully supported in test environment

## Future Improvements

- [ ] Add Next.js runtime mocking for full component tests (#2776)
- [ ] Add visual regression testing
- [ ] Add E2E testing with Playwright
- [ ] Increase coverage to 80%+

## Related Documentation

- [Vitest Documentation](https://vitest.dev/)
- [React Testing Library](https://testing-library.com/react)
- [MSW Documentation](https://mswjs.io/)
- [Happy-DOM](https://github.com/capricorn86/happy-dom)
