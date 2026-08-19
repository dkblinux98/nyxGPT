/**
 * Prop shape for the `react-virtuoso` stubs the component tests install.
 *
 * Fourteen test files replace `Virtuoso` with a plain `<div>` that renders
 * every row eagerly, and each one had typed its factory's parameter `any`.
 * That is 20-odd of the repo's eslint errors for one idea, and it also meant a
 * stub could destructure a prop the real component does not have and nothing
 * would say so.
 *
 * The union here is deliberately wide: it covers every prop any of those stubs
 * destructures, since a stub only pulls out what its own test exercises.
 *
 * Import it as `import type { ... }` -- `vi.mock` factories are hoisted above
 * the imports, so a *value* imported here would be undefined at factory time.
 * A type annotation is erased before that matters.
 */
import type React from 'react';

export type VirtuosoMockProps = {
  data?: unknown[];
  totalCount?: number;
  itemContent: (index: number, item?: unknown) => React.ReactNode;
  style?: React.CSSProperties;
  overscan?: number;
  defaultItemHeight?: number;
  startReached?: () => void;
  followOutput?: unknown;
  atBottomStateChange?: (atBottom: boolean) => void;
  components?: Record<string, React.ComponentType<Record<string, unknown>>>;
  'aria-label'?: string;
  role?: string;
};
