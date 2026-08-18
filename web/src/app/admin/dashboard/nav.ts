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
//
// `external` marks the single sanctioned exception to in-app, same-tab
// navigation (#3411): the SRE Overview tile launches the Grafana single
// pane of glass in a new browser tab instead of an in-app nyxGPT page --
// there is no in-app SRE Overview screen anymore. Its `href` here is the
// same-default fallback (`grafana_ui_url` unconfigured); AdminDashboardPage
// overrides it once `/api/v1/monitoring` reports the real configured URL.

export type AdminNavDest = {
  href: string;
  label: string;
  description: string;
  group: 'observation' | 'operation';
  external?: boolean;
};

// Kept in sync with docker/grafana/dashboards/sre-home.json's "uid".
export const GRAFANA_SRE_HOME_DASHBOARD_UID = 'nyxgpt-sre-home';

const DEFAULT_GRAFANA_UI_URL = 'http://localhost:3001';

export function grafanaSreHomeUrl(grafanaUiUrl: string): string {
  return `${grafanaUiUrl}/d/${GRAFANA_SRE_HOME_DASHBOARD_UID}`;
}

export const ADMIN_NAV: AdminNavDest[] = [
  {
    href: '/admin/health',
    label: 'System Health',
    description: 'Live service status, usage analytics, and resource metrics',
    group: 'observation',
  },
  { href: '/admin/infrastructure', label: 'Infrastructure Status', description: 'Detected deployment mode and per-component status', group: 'observation' },
  {
    href: '/admin/secrets',
    label: 'Guided Secrets Setup',
    description: 'Set write-once external tokens with masked entry and per-key help, and sync them to GitHub Actions',
    group: 'operation',
  },
  {
    href: '/admin/aws-credentials',
    label: 'AWS Credentials Setup',
    description: 'Guided AWS identity setup for cloud deploy -- routed to ~/.aws/credentials, the OS keychain, or an existing source, never config.ini',
    group: 'operation',
  },
  {
    href: '/admin/cloud-infrastructure',
    // No parentheses in the label: dashboard.test.tsx builds a RegExp from it
    // verbatim, so punctuation with regex meaning would never match its tile.
    label: 'AWS Cloud Infrastructure',
    description:
      'State of the AWS substrate and the release deployed onto it -- version, access tunnel, health, and deploy history. Deploying and tearing down are CLI operations (#3514)',
    // Observation, not operation, since the owner's #3514 decision made the
    // cloud surface status-plus-CLI-pointers: nothing on this page creates or
    // destroys cloud resources.
    group: 'observation',
  },
  // There is deliberately no Portability and Acceptance tile (#3803). #3516
  // added one, reading the Definition of Done as requiring a dashboard
  // surface for `nyxgpt ops portability`; the owner removed it because the
  // matrix describes the *product*'s portability claims, not this machine's
  // state -- nothing to observe, nothing to act on. `nyxgpt ops portability`
  // is the way to read the matrix. Do not re-add a screen that only restates
  // documentation.
  { href: '/admin/canary', label: 'Canary Operations', description: 'Deploy, gradual rollout, and promotion with automatic rollback', group: 'operation' },
  { href: '/admin/self-heal', label: 'Self-heal Operations', description: 'Watchdog that restarts unhealthy services', group: 'operation' },
  {
    href: grafanaSreHomeUrl(DEFAULT_GRAFANA_UI_URL),
    label: 'SRE Overview',
    description: 'Single pane of glass in Grafana: dashboards, logs, traces, and errors',
    group: 'observation',
    external: true,
  },
];
