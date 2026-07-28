// Admin dashboard quick-nav destinations.
//
// Lives in its own module (not page.tsx) because Next.js page files may only
// export a default component plus a fixed set of framework exports -- an
// arbitrary named export like ADMIN_NAV fails the Next.js Page type check and
// breaks `next build`. Keeping it here lets both page.tsx and its test import
// it without violating that contract.
//
// `group` separates screens that only observe system state (rendered under
// System Status) from screens that configure or act on it (rendered under
// Configuration, alongside the Configuration Wizard). Every destination must
// declare a group explicitly -- there is no default -- so a future addition
// can't silently land in the wrong section.

export type AdminNavDest = {
  href: string;
  label: string;
  description: string;
  group: 'observation' | 'operation';
};

export const ADMIN_NAV: AdminNavDest[] = [
  { href: '/admin/health', label: 'System Health', description: 'Live status of every nyxGPT service', group: 'observation' },
  { href: '/admin/deploy', label: 'Deployment Operations', description: 'Blue/green switch and rollback', group: 'operation' },
  { href: '/admin/infrastructure', label: 'Infrastructure Operations', description: 'Terraform and Kubernetes local deploys', group: 'operation' },
  { href: '/admin/canary', label: 'Canary Operations', description: 'Gradual rollout with automatic rollback', group: 'operation' },
  { href: '/admin/self-heal', label: 'Self-heal Operations', description: 'Watchdog that restarts unhealthy services', group: 'operation' },
  { href: '/admin/observability', label: 'SRE Overview', description: 'Monitoring, logs, tracing, and error tracking', group: 'observation' },
  { href: '/admin/analytics', label: 'Usage Analytics', description: 'Chat and RAG usage over time', group: 'observation' },
  { href: '/settings', label: 'Full Metrics', description: 'Resource metrics in Settings', group: 'observation' },
];
