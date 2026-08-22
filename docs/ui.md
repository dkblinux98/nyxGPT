# UI

This document describes the local UI surface provided by **nyxGPT**:

- **Local Web UI** — a lightweight Next.js application backed by FastAPI

The web UI depends on the FastAPI backend and its streaming chat endpoints.

---

## Backend requirement (FastAPI)

The web UI requires the FastAPI backend to be running.

The backend is normally managed via the `nyxgpt ops` command:

```bash
# Install and start all services (including API)
nyxgpt ops install

# Restart just the API service
nyxgpt ops restart api

# Check system health
nyxgpt ops doctor
```

Verify the API is running:

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/v1/info
```

Interactive API docs (local only):

```bash
open http://127.0.0.1:8000/docs
```

---

### RAG Controls (Web UI)

The underlying RAG mechanics (config, filters, ingestion, citations export)
are documented in [RAG](rag.md) and [RAG — Per-Session RAG Control](rag.md#per-session-rag-control);
this section covers the UI surface specifically.

**Web UI** — RAG controls sit left of the message input in the chat interface:
- **RAG Toggle** button to enable/disable RAG for the current session
- RAG status displays current state (ON/OFF)
- **Document Filters** button (available when RAG is enabled) to narrow which documents are searched: select specific documents by checkbox, filter by filename (partial match, case-insensitive) or ingestion date range, and pick a **Collection** to scope retrieval (populated from [RAG Collections management](rag.md#collections-management-ui); defaults to `"default"` when none is picked). The button label always shows the effective collection (e.g. `Filters · default`) so it's never implicit. The **Select Documents** checkbox list is scoped to the currently selected collection and re-fetches whenever the collection changes, so it never shows another collection's documents; an empty collection renders an honest empty list (#3566). Switching the **Collection** dropdown also clears any previously-checked document selection, since a document checked under one collection doesn't belong to another — carrying it over would silently pin retrieval to `collection=X AND doc_ids=[doc-not-in-X]`, guaranteeing zero results every turn (#3585). Filename Search filters that same list live, as-you-type (no Enter or button needed); a query that matches nothing shows an explicit "No documents match" state, distinct from the "no documents in this collection" empty state. The date range is optional and never blocks filtering. The filters themselves only scope *search*; the panel's separate upload button (see below) is what ingests documents. Filters persist across page reloads via session storage, with an active-filter indicator when applied.

The Document Filters panel also includes an **"Upload document to '\<collection\>'"** button
(Owner decision, 2026-08-02, reversing the 2026-08-01 design above per stakeholder acceptance
testing of #3463): selecting a file ingests it directly into the collection currently selected
in the Collection dropdown (`"default"` when none is picked), refreshing both the collection list
and the Select Documents list on success — no page reload or trip to the Collections admin page
required. This is distinct from the chat paperclip, which remains a pure conversation attachment
(see [File Attachments](#file-attachments) below) that never touches the RAG store. Documents can
still also be uploaded via **Upload Document** on the [RAG Collections](rag.md#collections-management-ui)
admin page (`/admin/collections`).
- **RAG Citations** displayed inline with responses: retrieved source chunks with click-to-expand for full text, relevance scores with quality indicators (High/Medium/Low), document IDs, human-readable chunk position ("chunk 2 of 5", not the internal zero-based index), and source collection (shown when it isn't `"default"`), plus export to separate files (JSON, Markdown). When RAG was on for a turn but retrieval returned zero chunks, the citations block is replaced with an explicit **"No RAG sources retrieved for this reply"** indicator rather than showing nothing — this keeps an uncited answer from being mistaken for a cited one when the model actually answered from conversation history (#3585).

Pinned sessions are displayed with a 📌 icon and appear at the top of the list.

---

## Sessions API (UI-critical)

The web UI depends on session primitives for listing and persisting conversations.

- **List sessions**

```bash
curl -s http://127.0.0.1:8000/api/v1/sessions
```

Response shape:

```json
{
  "sessions": [
    {
      "name": "session-name",
      "title": "Session Title",
      "message_count": 10,
      "last_modified": "2026-01-12T12:00:00Z"
    }
  ]
}
```

- **Initialize a session**

Session creation is idempotent and does **not** trigger a model call. This allows UI bootstrapping without side effects.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"name": "my-session"}'
```

---

## Streaming chat (UI-critical)

The web UI relies on the streaming endpoint:

```
POST /api/v1/chat/stream
```

This endpoint:

- yields text chunks incrementally
- persists assistant and user messages to the active session
- optionally injects RAG context before streaming begins

**Important:**
UI clients must treat this response as a stream, not as a single JSON payload.

---

## Local Web UI (Next.js)

The local web UI is a Next.js application located in `web/`.

### Running via nyxgpt ops (recommended)

The web UI can be launched via the `nyxgpt ops` command:

```bash
# Install and start all services (including web UI)
nyxgpt ops install

# Restart just the web UI
nyxgpt ops restart web

# Check system health
nyxgpt ops doctor
```

Once running, open:

```bash
open http://127.0.0.1:3000
```

### Configuration

The web UI reads its runtime configuration from:

```text
web/.env.local
```

Typical values:

```env
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
```

### Web UI Features

The web UI includes:

- **Chat interface** with streaming responses and session management
- **Message editing and regeneration** — edit any message and fork the conversation from that point, or regenerate assistant responses (see [Message Editing](api.md#message-editing))
- **RAG toggle and search scoping** in the chat interface, plus per-message RAG citations (see [RAG](rag.md)); the Document Filters panel also lets a user upload a document directly into the selected collection from the chat page itself, in addition to the [RAG Collections](rag.md#collections-management-ui) admin page
- **Admin dashboard** (`/admin/dashboard`) — a unified hub with a system status overview (canary state, resource metrics, opt-in observability stacks), a configuration summary, access/API-key management (view masked key, enable/disable auth, rotate), and an activity log (audit trail of admin actions)
- **System Health screen** (`/admin/health`) — the single consolidated destination (#3413) for "how is the system doing," with three sections on one page: service health (service uptime, a live Self-Heal Components card naming any unhealthy self-heal-monitored component — sourced from the same `/api/v1/self-heal/status` the Self-Heal page uses, so the two pages can never disagree, with a link through to [Self-Heal](self-healing.md) for full detail — dependency reachability checks for Ollama and Cassandra when RAG is enabled, and alert indicators — live from Grafana's real alerting when monitoring is enabled and reachable, else a local threshold estimate; the panel labels which one it's showing, see [alerting.md](alerting.md#system-health-panel)), usage analytics (total/per-model/per-day request and token breakdowns from recorded chat usage, with JSON/CSV report export — see [API — Usage Analytics](api.md#usage-analytics)), and resource metrics (the history-backed live performance dashboard from #3352). Section anchor links at the top jump between them. The former standalone `/admin/analytics` route and the Settings page's Resource Usage tab were retired into this screen — it is now their only home.
- **Infrastructure Status** (`/admin/infrastructure`) — status-only reporting of the detected deployment mode, per-mode component state, and which instance serves traffic (#3410). Its Native card also carries the **install identity** (#3861): an `ARTIFACT INSTALL` / `DEV INSTALL` badge, and beneath it *which build* that is — the version, the channel (`stable`/`candidate`/`dev`), the service manager and the concrete name each component is registered under (`api=nyxgpt-api@3.0.0rc`). The badge alone reads the same for a 2.1.0 keg and a 3.0.0rc12 one, which is how four install identities accumulated unseen on one machine; when no identity was recorded — a machine whose marker predates them — the card says so and points at `nyxgpt ops doctor` rather than presenting the mode as if it identified the build (see [the install identity](ops.md#the-install-identity)). In dev mode it also names the checkout whose working tree `api`/`web` are running, plus the reminder that this stack is not exercising the artifact path (see [`--dev`](ops.md#--dev-run-the-current-checkout-without-an-artifact-build)). Its Kubernetes card carries the equivalent for that deployment (#3834): what the two images in the cluster were built from — the published artifacts, a checkout's working tree (`--dev`), or `unrecorded` when no marker exists for it on this machine, which is never presented as the artifact default. That card also badges every Pod **READY**, **PENDING** or **FAILED** rather than echoing `kubectl`'s line, which says `Pending` both for a Pod pulling its image and for one the node cannot fit; a failed Pod carries the scheduler's or kubelet's own reason (#3827, see [kubernetes.md](kubernetes.md#ready-pending-failed)). Like the rest of the page it reports only — switching modes is `nyxgpt up` / `nyxgpt up --dev` (`nyxgpt ops install --kubernetes [--dev]` for the cluster, `--terraform [--dev]` for the containers). The local Terraform card is labelled **Terraform (local containers)**, because it detects `nyxgpt-tf-*` containers on this machine and an AWS instance Terraform provisioned is a different thing, reported in the AWS section below (#3804). That card carries **its own** install mode, never the Native card's (#3835): `DEV IMAGES` when the api/web images were built from a named working tree, `ARTIFACT IMAGES` (with the image refs it is running — version-tagged since #3985, so the card names *which* release is serving) when they were built from the published `nyxgpt-api`/`nyxgpt-web` source tarballs, and `IMAGES NOT RECORDED` when containers are up but no install wrote the marker — a deployment predating that marker, or brought up outside `nyxgpt ops`, whose build genuinely is not known and so is not guessed at (see [Terraform — dev vs artifact](terraform.md)).
  - **AWS** — the same page's information-only section for the cloud: the substrate (region, instance, type, public IP, VPC, subnet, security group, key pair, open ports), the release deployed onto it, its **build source** — a published release, or `DEV BUILD` plus the checkout whose working tree `nyxgpt cloud deploy --dev` shipped there (#3950, see [Dev mode on a cloud target](cloud.md#dev-mode-on-a-cloud-target)), which every other field on the card reads identically for — the **connection target** (the SSH `user@host` and identity file the deploy recorded, plus the raw `ssh` the wrapped tunnel executes, shown as diagnostics — #3813), the access tunnel and a health answer, the Terraform state backend, the deploy/teardown history, and the wrapped `nyxgpt cloud …` commands rendered as text pointers from the backend's own `LIFECYCLE_COMMANDS` (including `nyxgpt cloud status`, and `nyxgpt cloud ops status` for the instance's own container state). **Nothing in it acts.** Which source answered depends on where the dashboard is running, and the page says so: instance metadata (IMDSv2) when it is served *from* the EC2 instance, Terraform state on the workstation that provisioned it, and **unknown** — never a blank "not provisioned" — on a machine that is neither (#3804). See [Cloud (AWS)](cloud.md#from-the-dashboard-information-only-3804).
- **RAG Collections management** (`/admin/collections`) for multi-model embedding support (see [RAG — Collections Management UI](rag.md#collections-management-ui))
- **RAG Playground** (`/admin/playground`) for interactive query testing and A/B comparison (see [RAG — RAG Playground](rag.md#rag-playground))
- **Model management** page (`/models`) for pulling, deleting, and viewing Ollama models
- **Configuration wizard** (`/admin`) covering every `config.ini` section (core/model, RAG, API & auth, observability) with apply-on-save reload and a restart offer for settings that need one
- **SRE Overview** tile (Admin Dashboard) — launches Grafana, the single pane of glass for every SRE signal (dashboards, logs, traces, errors), in a new browser tab; there is no in-app SRE Overview page (#3411, see [Observability — Grafana Single Pane of Glass](docker-compose.md#grafana-single-pane-of-glass))
- **Mobile-responsive layout** — the session sidebar collapses into a dismissible overlay below the `useIsMobile` breakpoint (768px), chat controls grow to touch-friendly tap targets, and inputs use a 16px minimum font size to prevent iOS Safari's auto-zoom-on-focus
- **Keyboard shortcuts** for productivity:
  - `Cmd/Ctrl+K` — Create new chat
  - `Cmd/Ctrl+/` — Toggle sidebar visibility
  - `/` — Focus search input
  - `Esc` — Close menus and dialogs

#### Model List Freshness

The admin models list and the chat model selector don't poll continuously, but re-fetch `/api/models` automatically so a model pulled from `/models` shows up without a manual page reload:

- **Admin page** (`/admin`) — re-fetches when the browser tab regains window focus or becomes visible again (`visibilitychange`).
- **Chat model selector** (`web/src/app/components/ChatPane.tsx`) — re-fetches on mount, every time the model dropdown is opened, and on window focus.

Because the trigger is focus/open rather than an immediate push, a model pulled in another tab won't appear in a *background* tab until that tab is focused or its dropdown is reopened.

#### Mobile Responsiveness

Below a 768px viewport width, the layout switches from a permanent two-column sidebar + chat view to a single-column view:

- The session sidebar (`web/src/app/page.tsx`) renders as a fixed-position overlay with a tap-to-dismiss backdrop instead of a static column, and starts collapsed by default.
- Selecting a session automatically closes the sidebar overlay so the chat is immediately visible.
- Icon buttons in the sidebar header and the chat input toolbar (`web/src/app/components/ChatPane.tsx`) expand to at least 44×44px touch targets.
- `input`, `textarea`, and `select` elements use a 16px minimum font size on small viewports (`web/src/app/globals.css`) to avoid the automatic zoom iOS Safari applies to focused inputs smaller than that.

The breakpoint detection lives in the `useIsMobile` hook (`web/src/hooks/useIsMobile.ts`), which tracks `window.matchMedia('(max-width: 767px)')` and starts as `false` so the first client render matches the server-rendered desktop markup before updating post-mount.

#### Toast Notifications

The web UI includes a toast notification system for user feedback:

- **Success notifications** — Confirm successful operations (session creation, model pull, etc.)
- **Error notifications** — Display error messages with context
- **Warning notifications** — Show warnings and non-critical issues
- **Info notifications** — Provide informational messages

Toasts appear in the bottom-right corner, auto-dismiss after 5 seconds (configurable), and can be manually dismissed by clicking the × button. Multiple toasts stack vertically.

#### File Attachments

The chat interface supports inline file attachments. Users can attach images and documents directly to a chat message before sending. This is a pure conversation attachment: the file's content is given to the model for that message only (summarize, discuss, quote) and is never chunked, embedded, or stored in any RAG collection — no collection is involved or modified, and attach behavior does not depend on the Collection filter above. To add a document to RAG for future retrieval, use the **"Upload document to '\<collection\>'"** button in the Document Filters panel (see [RAG Controls](#rag-controls-web-ui) above), or **Upload Document** on the [RAG Collections](rag.md#collections-management-ui) admin page.

**How to attach files:**

- **Paperclip button** — Click the 📎 paperclip icon in the chat input toolbar to open a file picker. Supported file types are filtered automatically.
- **Drag and drop** — Drag one or more files from the desktop or file manager and drop them onto the chat input area. A visual drop zone indicator appears while dragging.

**Thumbnail strip:**

Attached files are previewed in a horizontal thumbnail strip above the chat input:

- Image attachments show a miniature image preview.
- Document attachments (PDF, plain text) show a file-type icon and filename.
- Click the × on any thumbnail to remove that attachment before sending.

**Supported file types:**

| Type | MIME types |
|------|-----------|
| Images | `image/jpeg`, `image/png`, `image/gif`, `image/webp` |
| Documents | `application/pdf`, `text/plain` |

**Size limit:** Attachments are capped at approximately 20 MB per file.

**How attachments are processed:**

- Image attachments are forwarded to the model's vision API (requires a multimodal Ollama model).
- Document attachments are decoded and their text content is prepended to the chat prompt before sending.

**API:** Attachments are sent as the `attachments` field on the `POST /api/v1/chat/stream` request body. See [api.md — AttachmentBlock Schema](api.md#attachmentblock-schema) for details.

---

#### Message Search

Search across all chat sessions with Ctrl+F (Cmd+F on Mac):

> **Shortcut labels and platform detection:** the chat page's shortcut hints
> (sidebar "Keyboard Shortcuts" panel, button tooltips) show `Ctrl` in the
> server-rendered HTML and switch to `⌘` on Macs immediately after the page
> mounts. Platform detection deliberately runs after mount rather than during
> render: reading `navigator.platform` while rendering made a Mac client's
> first render differ from the server HTML, which fails React hydration and
> leaves the page unresponsive.

- **Full-text search** — Search message content across all sessions
- **Filters** — Filter by message role (user/assistant/system)
- **Case-sensitive** — Toggle case-sensitive matching
- **Jump to context** — Click results to navigate to exact message in session
- **Match highlighting** — Visual highlighting of search terms in results
- **No-results feedback** — When neither sessions nor messages match, the
  dropdown stays open and shows "No results found" (rather than silently
  disappearing), so it is always clear the search ran
- **Historical messages** — Automatically loads and displays past conversations

The search interface appears as a modal overlay. Press Escape to close. Selecting a result switches to that session and scrolls to the matching message with a temporary highlight.

#### Settings

Access settings at `http://127.0.0.1:3000/settings` for day-to-day preferences and app info:

- **Appearance** — a Light/Dark theme toggle backed by the existing `ThemeContext` (persisted to `localStorage`), the same theme state used elsewhere in the app.
- **About** — read-only app info (version, default model, Ollama base URL, sessions directory) sourced from `/api/info`, plus a link to the [Configuration Wizard](#configuration-wizard) for changing model/RAG/logging configuration.

The Resource Usage tab (real-time memory/CPU/latency/queue metrics with
history charts) was removed from this page and relocated to the
[System Health screen](#admin-dashboard)'s Resource Metrics section (#3413),
which is now its only home. Observability dashboards (Grafana, Jaeger,
GlitchTip) live under their own [Observability](docker-compose.md#monitoring-dashboards)
page.

Settings has a `← Back to Admin Dashboard` link to `/admin/dashboard`, the same anchor used by every other admin/SRE page reached via the dashboard (health, self-heal, logs, etc.). Settings is also reachable directly from the chat UI's [Settings Menu](#settings-menu-sidebar); users who enter that way and want to return to chat should use browser Back rather than the in-page link, which always goes to the admin dashboard.

Back-nav follows two conventions depending on how a page is reached, not on its route path: admin/SRE pages reached via the admin dashboard (health, self-heal, canary, deploy, infrastructure, observability, settings, and — since #3396 removed it from the chat Settings menu — the [Configuration Wizard](#configuration-wizard), #3407) use `← Back to Admin Dashboard` targeting `/admin/dashboard`. Day-to-day user tools reached from the chat UI's Settings menu — Manage Models (`/models`), RAG Collections (`/admin/collections`), and RAG Playground (`/admin/playground`) — use `← Back to Chat` targeting `/`, even though the latter two live under the `/admin/` route path for historical reasons.

#### Settings Menu (Sidebar)

Click **⚙️ Settings** at the bottom of the sidebar to open the navigation menu that gates every admin/ops destination:

- **Admin** — a collapsible group. Clicking it expands in place (chevron rotates) to reveal: Dashboard, Manage Models, RAG Collections, and RAG Playground — the dashboard as the single admin entry point, plus the three day-to-day user tools. The Configuration Wizard, Resource Usage, Usage Analytics, Observability, Deployment, and Canary Rollout shortcuts were removed from this submenu (#3396); those destinations remain reachable from the [Admin Dashboard](#admin-dashboard)'s Configuration and System Status sections. The group stays open until you click a link, click outside the menu, or press `Escape` — clicking **Admin** itself only toggles the submenu and never closes the menu.
- **Support** — a collapsible group (#3745), sitting between Admin and Theme: **Docs** (`/support/docs`, the documentation packaged with this install — renders offline), then **File an Issue** (`/support/new`, nyxGPT's own intake page — the ticket type is asked there, not here). Both are plain in-app links: the group asks nothing and fetches nothing, which is why neither can end up pointing at github.com (#3811). Like Admin, it expands in place and toggling it never closes the menu. See [Support menu](#support-menu).
- **Theme** — Light/Dark toggle, same state as the [Settings page](#settings) appearance setting.

Clicking any link navigates and closes the menu. Clicking anywhere outside the menu (tracked via a ref on the menu container, not `stopPropagation`) or pressing `Escape` closes it without navigating.

#### Admin Dashboard

Access the dashboard at `http://127.0.0.1:3000/admin/dashboard` for an at-a-glance view of system state:

- **Status badges** — Canary, self-heal, observability, and auth status render as pill badges with a colored dot and label. The "on"/healthy state uses green; the "off"/idle state uses a gray dot and text (`var(--muted-foreground)`) that meets WCAG AA contrast against the pill background. (The Deploy badge was removed along with blue/green -- see #3409.)
- **Quick-nav tiles, split by observe vs operate** — the exported `ADMIN_NAV` list in `web/src/app/admin/dashboard/nav.ts` (a separate module because an arbitrary named export from a `page.tsx` fails the Next.js Page type check) tags every destination with a `group` of `observation` or `operation`, and each section renders only its group:
  - **System Status** shows the observation tiles: **System Health**, **Infrastructure Status**, and **SRE Overview** — screens that only report state. System Health is the single consolidated destination (#3413) for service health, usage analytics, and resource metrics; the former separate Usage Analytics and Full Metrics tiles were removed once their content moved onto that screen. Infrastructure moved here from Configuration when #3410 removed its Install/Destroy controls: `nyxgpt ops install|down --terraform|--kubernetes` are CLI-only now, so the page only reports the detected deployment mode, per-component status, and which instance is serving traffic (a single instance in native/Compose/Terraform mode, or a per-component -- `api` and `web` -- breakdown of stable/canary weight and per-track health in kubernetes mode, sourced from `canary.status()` for each, see #3419) — it doesn't change anything. In kubernetes mode it additionally reports the in-cluster observability layer per workload (Prometheus, Grafana, Loki, promtail, the OTel collector, Jaeger, GlitchTip -- #3787) and names the `nyxgpt ops port-forward --target observability` command that publishes their UIs on the ports the dashboard's own observability links use. Traffic *control* still lives on the Canary Operations page, linked from here. The AWS substrate and the release deployed onto it are a section *of* this page, not a screen of their own: #3514 removed the cloud screen's Apply/Deploy/Destroy controls and #3804 removed the screen along with everything that was still acting on it (Plan, Terraform state migrate/versions/restore/unlock, tunnel start/stop), folding what was left in here. A UI cannot safely change the substrate it is running on, and the second nyxGPT that could drive it safely collides with the first on `:8000`/`:3000`; cloud lifecycle is `nyxgpt cloud …`, which the section names. **SRE Overview** is the sanctioned exception to same-tab, in-app navigation (#3411): its tile opens the Grafana single pane of glass (the "SRE Home" dashboard, built from `grafana_ui_url`) in a new browser tab, marked with a `↗` decoration and `target="_blank"` — there is no in-app SRE Overview page.
  - **Configuration** shows the operation tiles — **Canary Operations** and **Self-heal Operations** — alongside the **Configuration Wizard**, all in the same tile grid. The guided **Secrets** and **AWS Credentials** setup screens were removed (#3805): credential *entry* does not belong in a browser (the value crosses an HTTP request and the page's process, and over a cloud access tunnel it would cross that path too), and by the time this dashboard is running, reaching it already required the secrets those screens collected. Below the tiles the card now names the wrapped commands as text, not controls — `nyxgpt secrets setup`, `nyxgpt ops secrets-sync`, `nyxgpt cloud credentials-setup` — the same pointer pattern as the cloud surface (#3514). The Configuration Wizard is unaffected, including its `[auth] api_key` rotation field: it remains the in-product configuration surface for a *running* system. These screens change something (deploy/gate/promote/roll back a canary, toggle or trigger self-heal), so they live under Configuration rather than System Status. (The **Deployment Operations** blue/green tile was retired -- canary is the sole deployment model, see #3409.) The "Operations" suffix on these tiles disambiguates them from the same-named Grafana dashboard links reachable from the SRE Home dashboard, which observe the same subsystems rather than act on them. **Canary Operations** (`/admin/canary`) has an `api`/`web` tab (#3419) that switches which component's stable/canary pair the page shows and controls -- the deploy/start/evaluate/promote/rollback actions and the stable/canary health cards apply to whichever component tab is selected. The vitals panel is per *track* (#3829): one row for the canary track -- the exact input Evaluate gates on, read from the Pods labelled `track=canary` rather than from the API process serving the page -- and one for the stable track while a rollout is in progress, each showing the Pods it was measured from or the reason it could not be measured. Promote offers a "Promote despite no canary traffic" override, shown only when the canary track is measurable and measurably at zero, since the backend refuses to promote a build no request has reached.

  Every tile shows the destination name plus a one-line description of what that screen is for, with the same description as a hover tooltip. Tiles navigate in the same tab, carry no arrow decoration, and hover/highlight using the theme CSS variables so light and dark modes both work -- except the SRE Overview tile, described above.
- **Pending-restart notice** (#3407, #3806) — stacked directly above the Configuration card, same width, outside it, and rendered by the same component the Configuration Wizard mounts, so the two always say the same thing. Shown whenever a saved config value differs from the value a service is still running with — whether it was changed in the wizard or from the CLI (`nyxgpt secrets setup`), because the state is shared (`~/.nyxGPT/pending-restart.json`, read via `GET /api/v1/infra/restart-status`). It states that the value **is saved but not yet in effect**, lists the component(s) and the `section.key` fields waiting, and how long they have been waiting.

  It is deliberately **not** a toast: it survives a reload, navigating away and back, and an API restart, and disappears only when the restart happens or the value is changed back to what the service is running. Restarting is explicitly optional and can be deferred indefinitely; the equivalent `nyxgpt ops restart <service>` is shown for reference (the wrapper rule — no raw `docker`/`brew`/`kubectl` command is ever shown).

  Clicking **Restart now** calls `POST /api/v1/infra/restart-required`, which restarts every pending component mode-aware (native/Compose/Terraform/Kubernetes, reusing the same dispatcher as self-heal's manual "Heal Now" — see [Self-Healing](self-healing.md)). When `web` is among the pending components, the notice says up front that restarting drops this browser session — and confirms before doing it — because the page is served by the service about to go down; without that it looks like a hang. The button then polls restart-status every second (up to 90 attempts) and reports **Restarting…** → the notice disappearing on success, or a "did not report finished in time" message (notice stays, retryable) if it times out. The budget is sized for the restarted service to *come back*, not for the restart command to return: an `api` entry is retired by the replacement API process at the end of its own startup, since the restart kills whatever was going to report it finished (#3806).
- **Query Cache panel** (`QueryCacheStatsPanel`) — hit rate, hits, misses, size, backend, and TTL for the RAG query result cache (`GET /api/v1/rag/cache/stats`), plus a Clear Cache action. When the cache is disabled (`[cache] query_cache_enabled = false`), the panel shows a **Disabled** message instead of a zero-value stat grid and hides the Clear Cache button — an all-zeros grid would be indistinguishable from "enabled but unused," and Clear Cache has nothing to act on (#3412). The message links to the [Configuration Wizard](#configuration-wizard)'s cache settings (Additional Settings → RAG, retrieval & caching) to enable it.
- **Access Management** — Masked API keys wrap within their pane instead of overflowing it; revealing a key shows the full value, also wrapped. Toggling auth or rotating the key here is restart-required for the `web` tier, so it raises the same **pending-restart notice** the Configuration Wizard does — on this same page, with the Restart control (#3806).
- **Back to Chat link** — "← Back to Chat" uses an underlined inline-link style (`inlineLinkStyle`) for clear affordance. The Activity Log's former "View logs in Log Aggregation →" link was removed (#3411): it only pointed at the SRE Overview page, never at an actual log view -- use the SRE Overview tile to reach Grafana's Logs Drilldown instead.

#### Configuration Wizard

Access the wizard at `http://127.0.0.1:3000/admin` (#3354), reached from the
[Admin Dashboard](#admin-dashboard)'s Configuration tile grid, to configure
every section of `config.ini` (see
[`docs/configuration.md`](configuration.md#option-3-web-configuration-wizard-edit-an-existing-install)
for the full field-by-field reference). Its field list is **derived from
`example.config.ini`** (#3388) rather than hand-maintained, so a new config
option appears in the wizard automatically instead of the two silently
drifting apart. The page header reads `← Back to Admin Dashboard`, targeting
`/admin/dashboard`, matching the #3322/#3397 convention for dashboard-reached
admin pages (#3407).

1. **Core & Model** — Default model, chat timeout, sessions/vectorstore
   directories, log level/directory, and the Ollama backend URL
2. **RAG Configuration** — Enable/disable retrieval-augmented generation and
   configure its Cassandra connection (hosts, port, keyspace, table) and
   embedding model
3. **API & Auth** — API server host/port, API-key authentication
   (enable/disable, header name, rotate the key), and rate limiting
4. **Observability** — Tracing, error tracking, monitoring, and log
   aggregation, each with its own enable toggle and connection settings
5. **Additional Settings** — every other `example.config.ini` option not
   already covered by a step above, generated directly from the schema and
   grouped by topic (core behavior; API & network; RAG/context/pdf/caching;
   observability & self-heal; Kubernetes deployment). `[paths]`, `[openai]`,
   and `[github]` are excluded (agent-level concerns, not nyxGPT options).
   This step also shows a drift banner if `config.ini` has a key no longer
   declared in `example.config.ini`, with a **Remove** button per key.
6. **Summary** — Reviews the **entire** configuration Save would write,
   grouped by section, **derived from the same schema as the rest of the
   wizard** (#3407) — a section added to `example.config.ini` shows up here
   automatically, not just the four originally hand-built groups. Values
   changed this session show a "changed" badge; unchanged inherited defaults
   show a "default" badge (#3385); secret fields never render in cleartext,
   only their masked preview plus a "will be updated" badge if a new value
   was typed. Includes a link to the [System Health](#admin-dashboard) screen
   for live metrics — the wizard itself configures nothing there.

**Save changes / Cancel (#3407):** every step shows **Save changes** on the
left of the configuration box and **Cancel** on the right — not just the
Summary step, and not in the bottom Previous/Next nav. **Save** persists
every pending edit made anywhere in the wizard so far (not just the current
page) and advances: to the next step on any page before the last, or to the
**Admin Dashboard** from the Summary step (where it reads **Save
Configuration**). A save that fails (validation error, network error) stays
on the current page with the edits intact and an inline error instead of
navigating. **Cancel** always returns to the **Admin Dashboard**, discarding
only *unsaved* edits — a small note under the button says so explicitly.
Changes already committed by an earlier per-page Save are never reverted by
a later Cancel; it prompts for confirmation only if there are unsaved edits,
exiting immediately otherwise (#3387). The pre-existing **Previous**/**Next**
buttons in the bottom nav remain, for browsing between steps without saving.

Keyboard shortcuts:
- `←` / `→` — Navigate between steps (browsing only, doesn't save)
- `Enter` — Advance to next step, or save on the Summary step

**Configuration changes:** saving **merges** into `~/.nyxGPT/config.ini`
(still the single source of truth, #3194) rather than rewriting it — only
the keys you changed are updated or added at the line level, so comments,
key order, and anything the wizard doesn't manage (the excluded sections,
hand-added keys) survive untouched (#3388). The save applies immediately —
hot-reloadable settings (model, RAG, logging) take effect on the next
request with no restart. Settings that need a process bounce (API
host/port, the RAG Cassandra connection/embedding model, cache backends,
tracing/error-tracking/rate-limit config, the web UI's own host/port/API
base URL, and `[auth] enabled`/`api_key` for the web tier) carry an
explicit **activation classification** (#3806): each field shows it as a
hint *before* you save, and after saving the **pending-restart notice**
appears at the top of the wizard and on the [Admin
Dashboard](#admin-dashboard) (`GET`/`POST /api/v1/config/sections`,
`GET /api/v1/infra/restart-status`, `POST /api/v1/infra/restart-required` —
see [`docs/api.md`](api.md#config-wizard)). Enabling an observability toggle
also reconciles the matching Compose stack the same way `nyxgpt ops
observability` does, so it results in a working dashboard rather than a
dangling flag.

**Prerequisites:**
- FastAPI backend must be running (`nyxgpt ops install` or `nyxgpt ops restart api`)
- If configuration fails to load, verify the API is accessible at `http://127.0.0.1:8000/health`
- See [Troubleshooting](troubleshooting.md) for common issues

#### Log Viewer (removed)

The standalone raw-file log viewer (`/admin/logs`) has been removed now that the log aggregation
stack is fixed (#3349) — it duplicated what Grafana's own log views already provide. Read
application logs in Grafana's **Logs Drilldown** app instead (reached via the SRE Overview tile on
the Admin Dashboard, pre-filtered to `{job="nyxgpt"}` — see
[Observability — Grafana Single Pane of Glass](docker-compose.md#grafana-single-pane-of-glass)).
The chat/streaming endpoints log upstream Ollama errors (status, model, message) to `api.log`
before they reach the client, so that file under `~/.nyxGPT/logs` is still the first place to
check if the aggregation stack itself is down.

#### Virtual Scrolling (Performance Optimization)

The web UI uses **react-virtuoso** for efficient rendering of large message lists in chat sessions and session lists. This optimization ensures smooth performance even with thousands of messages or sessions.

##### Why react-virtuoso?

**Trade-offs considered:**

- **react-virtuoso** (chosen)
  - ✅ Dynamic item heights (messages vary in size)
  - ✅ Built-in scroll position management
  - ✅ TypeScript support
  - ✅ Smooth auto-scroll and manual scroll co-existence
  - ✅ Active maintenance and React 18+ support
  - ⚠️ Slightly larger bundle size (~15KB gzipped)

- **react-window** (alternative considered)
  - ✅ Smaller bundle size (~5KB gzipped)
  - ❌ Fixed item heights only (not suitable for variable-height messages)
  - ❌ Less flexible scroll management
  - ⚠️ Less active maintenance

- **react-virtual** (alternative considered)
  - ✅ Small bundle size
  - ✅ Dynamic heights
  - ❌ More manual configuration required
  - ❌ Less mature ecosystem

**Decision:** react-virtuoso was chosen because chat messages have variable heights (short replies vs. long code blocks) and require sophisticated scroll behavior (auto-scroll on new messages, maintain position on edits).

##### Performance Characteristics

**Without virtual scrolling:**
- 1000 messages = 1000 DOM nodes = ~5-10s render time, poor scroll performance
- Memory usage grows linearly with message count
- Browser struggles with layout recalculations

**With react-virtuoso:**
- 1000 messages = ~10-20 rendered DOM nodes (only visible + overscan)
- Render time: <100ms regardless of total count
- Constant memory usage (only renders viewport)
- Smooth 60fps scrolling

**Benchmark (1500 messages):**
- Initial render: <100ms
- Scroll performance: 60fps
- Memory footprint: ~5MB (vs ~50MB without virtualization)
- DOM nodes rendered: 10-20 (vs 1500)

##### Configuration Options

**ChatPane.tsx (Message List):**

```typescript
<Virtuoso
  ref={virtuosoRef}
  data={messages}
  defaultItemHeight={100}  // Estimated height for pre-rendering
  followOutput={() => (isAtBottomRef.current ? 'smooth' : false)}  // Conditional auto-scroll
  atBottomStateChange={(atBottom) => { isAtBottomRef.current = atBottom }}  // Track scroll position
  itemContent={(idx, m) => renderMessageItem(idx, m)}
/>
```

**Key settings:**
- `defaultItemHeight={100}` — Provides height hint for better initial rendering (avoids scroll jumps)
- `followOutput` — Auto-scrolls new messages only if user is at bottom (doesn't interrupt manual scrolling)
- `atBottomStateChange` — Tracks whether user has scrolled away from bottom
- `overscan={5}` (VirtualizedSessionList) — Pre-renders 5 items above/below viewport for smoother scrolling

**VirtualizedSessionList.tsx (Session Sidebar):**

```typescript
<Virtuoso
  totalCount={sessions.length}
  itemContent={renderItem}
  overscan={5}  // Pre-render 5 items for keyboard navigation
  style={{ flex: 1, minHeight: 0 }}  // Responsive flex layout
/>
```

##### Scroll Behavior

**Auto-scroll during streaming:**
- Only auto-scrolls if user is at bottom (`isAtBottomRef.current === true`)
- Uses `behavior: 'auto'` for instant scroll (no animation during streaming)
- Prevents infinite re-renders with ref-based tracking (`lastMessageCountRef`)

**Scroll position preservation:**
- Captures scroll state before message edit: `virtuosoRef.current?.getState()`
- Restores position after edit: `virtuosoRef.current?.restoreStateFrom(scrollState)`
- Same mechanism used for message regeneration

**Jump to message:**
- Search results can jump to specific message: `scrollToIndex({ index, align: 'center', behavior: 'smooth' })`
- Target message is highlighted for 2 seconds with yellow background

##### Error Handling and Fallback

**VirtuosoErrorBoundary** wraps the virtualized list to provide graceful degradation:

```typescript
<VirtuosoErrorBoundary
  sessionName={sessionName}
  messages={messages}
  itemContent={renderMessageItem}
>
  <Virtuoso ... />
</VirtuosoErrorBoundary>
```

**If Virtuoso fails to render:**
1. Error boundary catches the error and logs sanitized message (no stack traces exposed)
2. User sees error message with two options:
   - **Retry** — Attempts to re-render Virtuoso
   - **Use Fallback Mode** — Renders messages without virtualization (simple `.map()`)
3. Error state clears automatically when session changes
4. Telemetry sent to `window.telemetry.captureException()` if available

**Fallback mode:**
- Disables virtualization, renders all messages directly
- Shows warning banner: "⚠️ Rendering in fallback mode (virtual scrolling disabled)"
- Still functional but slower with large message lists
- Useful for debugging or when Virtuoso has compatibility issues

##### Debugging Virtual Scrolling Issues

**Common issues and solutions:**

1. **Scroll position jumps on message load**
   - Cause: `defaultItemHeight` too far from actual height
   - Fix: Adjust `defaultItemHeight` to match average message height
   - Diagnostic: Check if messages vary wildly in height

2. **Auto-scroll doesn't work during streaming**
   - Cause: `isAtBottomRef` not tracking correctly
   - Fix: Verify `atBottomStateChange` callback is firing
   - Diagnostic: Add `console.log(isAtBottomRef.current)` before scroll

3. **Scroll position lost on edit**
   - Cause: `getState()` or `restoreStateFrom()` not called
   - Fix: Ensure scroll state capture/restore logic in `saveEdit()` and `handleRegenerateResponse()`
   - Diagnostic: Check if `scrollStateRef.current` has value before restore

4. **Performance degradation with 1000+ messages**
   - Cause: Virtualization not active (rendering all items)
   - Fix: Verify Virtuoso is being used (not `.map()`)
   - Diagnostic: Check DOM node count in DevTools (should be ~20, not 1000+)

5. **Messages not rendering in tests**
   - Cause: Mock doesn't simulate Virtuoso behavior
   - Fix: Update test mock to render viewport items (see `tests/app/virtual-scrolling.test.tsx`)

**DevTools inspection:**
```javascript
// In browser console
const virtuoso = document.querySelector('[data-virtuoso-scroller]');
console.log('Rendered items:', virtuoso.childElementCount);  // Should be ~10-20
console.log('Total messages:', messages.length);  // Could be 1000+
```

**Enable Virtuoso debug logging:**
```typescript
// Add to ChatPane.tsx temporarily
<Virtuoso
  {...props}
  logLevel="debug"  // Logs scroll events, item rendering
/>
```

##### Testing Virtual Scrolling

Virtual scrolling tests are located in `web/tests/app/virtual-scrolling.test.tsx`.

**Test coverage includes:**
- ✅ Virtuoso integration and configuration
- ✅ Viewport rendering (only 10-20 items rendered from 1000+)
- ✅ Scroll position preservation during edits/regeneration
- ✅ Auto-scroll behavior during streaming
- ✅ Jump-to-message with highlighting
- ✅ Error boundary recovery
- ✅ Performance with 1500+ messages

**Running tests:**
```bash
cd web
npm test -- virtual-scrolling.test.tsx
```

**Mock configuration:**
The test mock simulates viewport rendering by limiting rendered items:
```typescript
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: any) => {
    const VIEWPORT_ITEMS = 10;
    const renderCount = Math.min(data?.length || 0, VIEWPORT_ITEMS);
    // Only render viewport items, not all data
    return data?.slice(0, renderCount).map(itemContent);
  }
}));
```

This ensures tests verify that virtualization prevents rendering all 1000+ items.

##### User-Facing Changes

**What users will notice:**
- **Faster page loads** — Sessions with 500+ messages load instantly
- **Smoother scrolling** — No lag when scrolling through long conversations
- **Responsive UI** — Chat interface remains responsive during streaming
- **Preserved scroll position** — Edits and regenerations maintain scroll position

**What users won't notice:**
- Virtual scrolling is invisible to users — messages appear and behave identically
- Auto-scroll behavior unchanged (still scrolls during streaming, respects manual scroll)
- All existing features (edit, regenerate, RAG citations, search) work identically

**Migration note:** Existing sessions and messages are fully compatible. No data migration needed.

---

#### Bundle Size Optimization

The web UI is kept lean via a small set of build-time practices:

- **Code splitting** — `ChatPane` and `VirtualizedSessionList` are loaded with
  `next/dynamic` (see `src/app/page.tsx`) so they ship in separate chunks
  fetched only when needed, not in the initial page payload. Each loader is
  wrapped in `withChunkTimeout` and each call site in a `ChunkErrorBoundary`,
  so a chunk that fails or never arrives shows "Failed to load the interface"
  instead of its loading placeholder forever (#3857). When the client bundle
  never runs at all — so no boundary and no timeout can fire — a
  dependency-free inline watchdog shipped inside the HTML
  (`src/lib/hydrationWatchdog.ts`, mounted once in `src/app/layout.tsx`) paints
  the same surface on any route after 20 s without hydration — see
  [service-worker-pwa.md](service-worker-pwa.md#chunks-that-never-arrive-3857).
- **Vendor chunk isolation** — `web/next.config.ts` splits `react-virtuoso`
  into its own `vendor-virtuoso` chunk so it's fetched only alongside the
  virtualized list, not on every route.
- **Tree shaking** — `experimental.optimizePackageImports` is enabled for
  `react-virtuoso` so only the imports actually used are bundled.
- **Dependency audit** — run `npx depcheck` from `web/` to confirm every
  declared dependency is still used before adding new ones.

**Analyzing the bundle:**

```bash
cd web
npm run analyze
```

This produces interactive treemaps at `web/.next/analyze/client.html`,
`nodejs.html`, and `edge.html` showing exactly what contributes to each
chunk's size (via `@next/bundle-analyzer`). Use this before/after adding a
new dependency or page to catch regressions.

---

## Support menu

The sidebar's **Settings → Support** group holds **Docs** and **File an
Issue**, and both exist for the same reason: someone who installed nyxGPT from
PyPI or Homebrew has no repository checkout, so neither the docs nor an
issue-reporting path would otherwise be reachable from the product.

**Docs** (`/support/docs`) renders the documentation that shipped with the
installed package. The product documents are package data inside the wheel
(`nyxgpt.resources/docs`, the mechanism #3621 introduced for the ops layer's
runtime data), resolved through `importlib.resources` and never relative to a
source tree — so the documents shown match the version that is running by
construction, and they render with no network access. Links *between*
documents are rewritten server-side onto `/support/docs/...` so the tree
browses as a unit; links to files that only a checkout has resolve to the
hosted copy on GitHub. The Markdown is rendered to HTML by the API
(`/api/v1/support/docs`, `/api/v1/support/docs/{slug}`) with active content
stripped before the page injects it.

**What ships, and how it is ordered (#3809).** Not everything under `docs/`:
the repository also documents how it builds itself — the agent loop, CI
process, contributor setup, this project's own GitHub tokens — and none of
that is help for using nyxGPT. Those documents stay in the repository and are
never symlinked into `nyxgpt/resources/docs/`, so they are absent from the
wheel rather than shipped and hidden. The selection is named, grouped and
ordered in `nyxgpt.support.DOC_SECTIONS` (Getting started → Using nyxGPT →
Configuration → Operating → Reference → Help) and the index page renders those
groups; `/api/v1/support/docs` returns them as `sections`. Because the
grouping is data and not inferred from filenames, a newly added packaged
document that no section lists fails `tests/unit/test_support_docs.py` instead
of quietly appearing at the end of a flat list.

**File an Issue** is one menu entry and one page: `/support/new`, rendered by
this Next.js app exactly as the docs viewer is. The page asks the ticket type,
a one-line summary and what happened; `POST /api/v1/support/tickets` files it
as a labeled issue; and the next screen the filer sees is a thank-you page
summarising the ticket with a link to it. The version and platform are not
asked — the running install knows both and attaches them. The filer does not
see github.com at any point.

That is the surface the owner accepted the issue against (#3811), and it took
three attempts. The first handed the user to GitHub's compose page, which
shows a person with a broken install this repository's *development* metadata
— assignees, dev labels, dev projects, milestones, a contributing-guidelines
footer, none of it theirs — and leaves them there. The second built the
backend and the form correctly but kept the menu deciding, from a runtime
probe (`can_submit` on `/api/v1/support/context`), whether to open the form or
navigate to GitHub anyway — so the type was asked twice, and every degraded
path of that probe still ended on github.com. The menu entry is now a plain
link with nothing to decide.

Filing needs a credential, so there is one case the product cannot cover: an
install with no `[github] pat`. The page says so in place, and offers the
prefilled GitHub form as a link the filer may choose — the same offer it makes
if filing fails outright. It is the honest answer to "this install cannot file
for you", and it is an offer rather than a redirect. Whether a hosted intake
should remove even that case is the owner's call (ledger `Q-006`); nothing in
this design forecloses it.

Either path applies the `Support` label, which is what routes the ticket to
the Support project and away from the agent loop. On the path nyxGPT files
itself the label is **read back from the created issue** rather than assumed:
GitHub silently drops `labels` for a token without push access, and an
unlabeled support ticket is exactly what #3810 was.

The type is asked in nyxGPT rather than on GitHub because nothing would
otherwise record it (#3811): the Support project types tickets with a
`Ticket Type` project field, and GitHub maps a form answer to neither a label
nor a project field — `labels:` is a static template-level list and a dropdown
answer lands in the issue body. Nor does the answer survive being handed to
GitHub: the version that passed `ticket_type` as a query-parameter prefill was
re-tested with the dropdown arriving as **None**, which is why the question is
asked where the ticket is actually created. So the answer renders into the body under its
own heading (whichever path filed it), and the owner sets the project field
(and `Priority`, which is a judgement about the queue that the filer cannot
make) at triage. A ticket nyxGPT files is deliberately shaped like one the
form produces — same `###` headings, same order — so triage reads one format
rather than two, and `support_intake_guard.yml` recognises both.

The label routes the report onto the separate **nyxGPT Support** project and
keeps it out of the agent delivery loop entirely: no code-project item, no
field stamping, no sprint, no selection. See
`scripts/agents/lib/support_label.py`. On the fallback path the template
declares it (a `labels=` URL parameter would be silently dropped for a filer
without write access — exactly the filer that form is for).

All of which holds only while the label EXISTS — GitHub drops a
template-declared label that does not, without erroring, and the form goes on
accepting tickets that route nowhere and read to every guard as ordinary work.
That is how #3810 was assigned to the scrummaster seven seconds after a user
filed it. Two mechanisms now stand behind the label:
`admin_ensure_support_label.yml` re-asserts it on a schedule and on any push
touching the form, then verifies it by reading the label list back; and
`support_intake_guard.yml` catches a support-shaped issue that arrives without
it, relabels the ticket, removes any agent assignee, and fails the run on
purpose so the degraded path is never quiet.

---

## Operational dependencies

For reliable UI operation, ensure the following are active:

- **Docker Desktop** (required for Cassandra)
- **Cassandra container** (`nyxgpt-cassandra`)
- **FastAPI backend** (`nyxgpt-api`)
- **Web UI service** (`nyxgpt-web`)

Logs from all components are available under:

```text
~/.nyxGPT/logs
```

For installation, startup, and diagnostics, see:

> **docs/api.md → Operational Tasks**
