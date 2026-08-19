import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'happy-dom',
    setupFiles: ['./tests/setup.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html'],
      include: ['src/**/*.{ts,tsx}'],
      // 100% gate (#3266): regressions fail the build. Genuinely-untestable
      // files must be added to `exclude` with a comment, never by lowering
      // these thresholds.
      thresholds: {
        statements: 100,
        branches: 100,
        functions: 100,
        lines: 100,
      },
      exclude: [
        'node_modules/',
        'tests/',
        '**/*.config.{js,ts}',
        '**/*.d.ts',
        '.next/',
      ],
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      // Next.js resolves `next/dynamic` differently per router: everything
      // under app/ is aliased to the App Router implementation
      // (`next/dist/api/app-dynamic`, see Next's create-compiler-aliases.js),
      // which is React.lazy + Suspense. The bare `next/dynamic` entry point
      // is the Pages Router loadable, and the two disagree on the case that
      // matters here: the Pages one catches a rejected chunk import and keeps
      // rendering its `loading` fallback, while the App Router one lets the
      // rejection reach an error boundary. This app is App Router only, so
      // without this alias the tests exercise semantics the product never
      // runs -- which is how #3857's permanent placeholders stayed untested.
      'next/dynamic': 'next/dist/api/app-dynamic',
    },
  },
});
