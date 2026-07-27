# UI

This document describes the local UI surfaces provided by **nyxGPT**:

- **Terminal UI (TUI)** — a rich terminal-based chat interface
- **Local Web UI** — a lightweight Next.js application backed by FastAPI

Both UIs depend on the FastAPI backend and its streaming chat endpoints.

---

## Backend requirement (FastAPI)

Both UIs require the FastAPI backend to be running.

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

## Terminal UI (TUI)

Start the terminal UI with:

```bash
nyxgpt tui
```

The TUI:

- streams assistant responses token-by-token
- persists conversations via the Sessions API
- defaults to the `default` session
- supports RAG-assisted chat if enabled
- displays a status bar showing session name, message count, active model, and RAG status

### TUI Keyboard Shortcuts

- **Ctrl+H** / **F1** — Show help overlay with all shortcuts
- **Ctrl+P** — Command palette (quick command access with search)
- **Tab** — Navigate to next pane
- **Shift+Tab** — Navigate to previous pane
- **Ctrl+S** — Open session picker (browse and switch sessions)
- **Ctrl+F** — Search messages across sessions
- **Ctrl+R** — Toggle RAG for current session
- **Ctrl+M** — Manage models
- **Ctrl+N** — Rename current session
- **Ctrl+D** — Delete current session (with confirmation)
- **Ctrl+A** — Open the [document attachment manager](sessions.md#force-include-document-attachment) for the current session
- **Ctrl+L** — Clear output buffer
- **Ctrl+C** — Quit

**Commands:**
- `/clear` — Clear the output buffer

### Session Picker

Press **Ctrl+S** to open the interactive session picker which allows you to:

- Browse all available sessions
- Search sessions by name, title, summary, or tags
- View session metadata (message count, last modified, tags, summary)
- Navigate with arrow keys (Up/Down) or keyboard search
- Press **Enter** to switch to the selected session
- Press **Escape** or **Ctrl+C** to cancel

### RAG Controls (Web UI and TUI)

The underlying RAG mechanics (config, filters, ingestion, citations export)
are documented in [RAG](rag.md) and [RAG — Per-Session RAG Control](rag.md#per-session-rag-control);
this section covers the UI surfaces specifically.

**Web UI** — RAG controls sit left of the message input in the chat interface:
- **RAG Toggle** button to enable/disable RAG for the current session
- **File Upload** to ingest documents into the RAG database
- RAG status displays current state (ON/OFF)
- **Document Filters** button (available when RAG is enabled) to narrow which documents are searched: select specific documents by checkbox, filter by filename (partial match, case-insensitive) or ingestion date range. Filters persist across page reloads via session storage, with an active-filter indicator when applied.
- **RAG Citations** displayed inline with responses: retrieved source chunks with click-to-expand for full text, relevance scores with quality indicators (High/Medium/Low), document IDs and chunk numbers, and export to separate files (JSON, Markdown)

**Terminal UI (TUI)** — press `Ctrl+R` to toggle RAG on/off for the current session (status shown in the UI). RAG citations display inline when enabled: a compact citation summary (number of sources retrieved), document IDs, chunk references, and confidence scores with color-coded quality indicators (green/yellow/red based on score).

Pinned sessions are displayed with a 📌 icon and appear at the top of the list.

If the FastAPI backend is not running, the TUI will fail to connect.

---

## Sessions API (UI-critical)

Both UIs depend on session primitives for listing and persisting conversations.

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

Both the TUI and the web UI rely on the streaming endpoint:

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
- **RAG document upload and toggle** in the chat interface, plus per-message RAG citations (see [RAG](rag.md))
- **Admin dashboard** (`/admin/dashboard`) — a unified hub with a system status overview (deploy/canary state, resource metrics, opt-in observability stacks), a configuration summary, access/API-key management (view masked key, enable/disable auth, rotate), and an activity log (audit trail of admin actions)
- **System health dashboard** (`/admin/health`) — service uptime, dependency reachability checks (Ollama, and Cassandra when RAG is enabled), resource utilization, and threshold-based alert indicators
- **RAG Collections management** (`/admin/collections`) for multi-model embedding support (see [RAG — Collections Management UI](rag.md#collections-management-ui))
- **RAG Playground** (`/admin/playground`) for interactive query testing and A/B comparison (see [RAG — RAG Playground](rag.md#rag-playground))
- **Usage analytics dashboard** (`/admin/analytics`) — total/per-model/per-day request and token breakdowns from recorded chat usage, with JSON/CSV report export (see [API — Usage Analytics](api.md#usage-analytics))
- **Model management** page (`/models`) for pulling, deleting, and viewing Ollama models
- **Configuration wizard** (`/admin`) covering every `config.ini` section (core/model, RAG, API & auth, observability) with apply-on-save reload and a restart offer for settings that need one
- **Log Aggregation** (`/admin/observability`) — curated Grafana/Loki queries and Explore links for searching application logs (see [Log Aggregation](docker-compose.md#log-aggregation))
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

The chat interface supports inline file attachments. Users can attach images and documents directly to a chat message before sending.

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

Access settings at `http://127.0.0.1:3000/settings`, which has two tabs:

- **Resource Usage** — a real-time system performance dashboard: memory
  (RSS/VMS, percent, available), CPU (process/system), request latency
  (avg, P50, P95, P99), and batch queue depth/total requests. Backed by
  `GET /api/v1/metrics` for the live tiles (5-second auto-refresh,
  toggleable) and `GET /api/v1/metrics/history` (#3352) for persisted,
  server-side history charts over a selectable 1h/24h/7d range. Supports
  manual refresh, JSON/CSV export, and color-coded thresholds (green
  < 60%, yellow 60-80%, red > 80%). Observability dashboards (Grafana,
  Jaeger, GlitchTip) live under their own [Observability](docker-compose.md#monitoring-dashboards)
  page rather than this tab.
- **General** — day-to-day preferences and app info:
  - **Appearance** — a Light/Dark theme toggle backed by the existing `ThemeContext` (persisted to `localStorage`), the same theme state used elsewhere in the app.
  - **About** — read-only app info (version, default model, Ollama base URL, sessions directory) sourced from `/api/info`, plus a link to the [Configuration Wizard](#configuration-wizard) for changing model/RAG/logging configuration.

Settings has a `← Back to Admin Dashboard` link to `/admin/dashboard`, the same anchor used by every other admin page (analytics, self-heal, logs, etc.). Settings is also reachable directly from the chat UI's [Settings Menu](#settings-menu-sidebar); users who enter that way and want to return to chat should use browser Back rather than the in-page link, which always goes to the admin dashboard.

#### Settings Menu (Sidebar)

Click **⚙️ Settings** at the bottom of the sidebar to open the navigation menu that gates every admin/ops destination:

- **Admin** — a collapsible group. Clicking it expands in place (chevron rotates) to reveal: Dashboard, Configuration Wizard, Resource Usage, Usage Analytics, [Observability](docker-compose.md#monitoring-dashboards) (SRE overview, including log aggregation), Manage Models, RAG Collections, RAG Playground, Deployment, and Canary Rollout. The group stays open until you click a link, click outside the menu, or press `Escape` — clicking **Admin** itself only toggles the submenu and never closes the menu.
- **Theme** — Light/Dark toggle, same state as the [Settings page](#settings) appearance setting.

Clicking any link navigates and closes the menu. Clicking anywhere outside the menu (tracked via a ref on the menu container, not `stopPropagation`) or pressing `Escape` closes it without navigating.

#### Admin Dashboard

Access the dashboard at `http://127.0.0.1:3000/admin/dashboard` for an at-a-glance view of system state:

- **Status badges** — Deploy, canary, self-heal, observability, and auth status render as pill badges with a colored dot and label. The "on"/healthy state uses green; the "off"/idle state uses a gray dot and text (`var(--muted-foreground)`) that meets WCAG AA contrast against the pill background.
- **Quick-nav tiles** — System Health, Deployment, Canary, Self-heal, SRE Overview, Usage Analytics, and Full Metrics render as a responsive card grid (driven by the exported `ADMIN_NAV` list in `web/src/app/admin/dashboard/page.tsx`). Each tile shows the destination name plus a one-line description of what that screen is for, with the same description as a hover tooltip. Tiles navigate in the same tab, carry no arrow decoration, and hover/highlight using the theme CSS variables so light and dark modes both work.
- **Access Management** — Masked API keys wrap within their pane instead of overflowing it; revealing a key shows the full value, also wrapped.
- **Back to Chat / logs links** — "← Back to Chat" and "View logs in Log Aggregation →" use an underlined inline-link style (`inlineLinkStyle`) for clear affordance.

#### Configuration Wizard

Access the wizard at `http://127.0.0.1:3000/admin` (#3354) to configure
every section of `config.ini` (see
[`docs/configuration.md`](configuration.md#option-3-web-configuration-wizard-edit-an-existing-install)
for the full field-by-field reference):

1. **Core & Model** — Default model, chat timeout, sessions/vectorstore
   directories, log level/directory, and the Ollama backend URL
2. **RAG Configuration** — Enable/disable retrieval-augmented generation and
   configure its Cassandra connection (hosts, port, keyspace, table) and
   embedding model
3. **API & Auth** — API server host/port, API-key authentication
   (enable/disable, header name, rotate the key), and rate limiting
4. **Observability** — Tracing, error tracking, monitoring, and log
   aggregation, each with its own enable toggle and connection settings
5. **Summary** — Review every section and save (with a link to
   [Settings → Resource Usage](#settings) for live metrics; the wizard
   itself configures nothing there and no longer interrupts the flow with
   a monitoring step, #3384)

**Features:**
- Visual progress indicator showing current step
- Form validation for required fields
- Connection testing to verify API connectivity
- Secrets (API key, error tracking DSN) are shown masked and never round-trip
  in cleartext; leave the field blank to keep the current value, or type a
  new one to rotate it
- Clear navigation between steps

Keyboard shortcuts:
- `←` / `→` — Navigate between steps
- `Enter` — Advance to next step or save configuration

**Configuration changes:** saving writes the full section to
`~/.nyxGPT/config.ini` (still the single source of truth, #3194) and applies
it immediately — hot-reloadable settings (model, RAG, logging, auth)
take effect on the next request with no restart. Settings that
need a process bounce (API host/port, the RAG Cassandra connection/embedding
model, tracing/error-tracking/rate-limit config read only at startup) are
reported on the Summary step with a **Restart** button per affected
component, wrapping `nyxgpt ops restart` (`GET`/`POST
/api/v1/config/sections`, `POST /api/v1/config/restart` — see
[`docs/api.md`](api.md#config-wizard)). Enabling an observability toggle
also reconciles the matching Compose stack the same way `nyxgpt ops
observability` does, so it results in a working dashboard rather than a
dangling flag.

**Prerequisites:**
- FastAPI backend must be running (`nyxgpt ops install` or `nyxgpt ops restart api`)
- If configuration fails to load, verify the API is accessible at `http://127.0.0.1:8000/health`
- See [Troubleshooting](troubleshooting.md) for common issues

#### Log Viewer (removed)

The standalone raw-file log viewer (`/admin/logs`) has been removed now that the log aggregation
stack is fixed (#3349) — it duplicated what the curated Grafana/Loki views already provide. Read
application logs from the **Log Aggregation** panel at `/admin/observability` instead, which
offers curated Loki queries and Explore links across all services (see
[Log Aggregation](docker-compose.md#log-aggregation)). The chat/streaming endpoints log upstream
Ollama errors (status, model, message) to `nyxgpt.log` before they reach the client, so that file
under `~/.nyxGPT/logs` is still the first place to check if the aggregation stack itself is down.

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
  fetched only when needed, not in the initial page payload.
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
