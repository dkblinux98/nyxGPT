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
  // There are deliberately no Guided Secrets Setup or AWS Credentials Setup
  // tiles (#3805). #3505 and #3512 added them; the owner removed them because
  // a browser is a worse surface for credential *entry* than a terminal (the
  // value crosses an HTTP request and the page's process, and over a cloud
  // access tunnel it would cross that path too), and because by the time this
  // dashboard is running, reaching it already required the secrets those
  // screens collected. `nyxgpt secrets setup`, `nyxgpt ops secrets-sync` and
  // `nyxgpt cloud credentials-setup` are the surfaces; AdminDashboardPage
  // names them as text under Configuration. Do not re-add a screen that takes
  // a credential as input. The Configuration Wizard, including its
  // `[auth] api_key` rotation field, is unaffected.

  // There is deliberately no AWS Cloud Infrastructure tile (#3804). The AWS
  // substrate and the release deployed onto it are now an information-only
  // section *inside* Infrastructure Status, because a separate screen implied
  // a separate thing to operate. Cloud lifecycle is `nyxgpt cloud ...` and
  // nothing else: a UI served by the instance cannot safely change the
  // substrate it runs on, and the second nyxGPT that could drive it safely
  // collides with the first on :8000/:3000. Do not re-add the screen.

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
