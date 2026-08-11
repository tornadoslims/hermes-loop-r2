# hermes-loop-r2 Web UI

Dark-themed admin interface for the hermes-loop-r2 daemon. Static HTML/CSS
with vanilla JS — no build step, no framework. Pages load from `file://` or
via the embedded HTTP server (`loop.webui.WebUIServer`).

## Pages

| Page | File | Description |
|------|------|-------------|
| Dashboard | `dashboard.html` | Daemon status, queue depth, pipeline health, recent passes + stages tables |
| Issues | `issues.html` | Agent-ready issue table with ID, title, priority badge, status badge |
| Passes | `passes.html` | Pass history table with outcome badges, duration, timestamp |
| Plugins | `plugins.html` | Plugin table with toggle switches, version, stage, status badges |

## Shared structure

Every admin page shares the same shell:

| Element | Selector | Role |
|---------|----------|------|
| Top nav | `#topnav` | Fixed top bar: brand logo, nav links (Dashboard/Issues/Passes/Plugins), hamburger |
| Shell | `#shell` | Flex wrapper for sidebar + content |
| Sidebar | `#sidebar` | Left sidebar: Overview (Dashboard), Work (Issues, Passes), System (Plugins) |
| Content | `#content` | Main scrollable content area |
| Menu toggle | `#menu-toggle` | Mobile hamburger button, wired by `app.js` |

## Data-binding points

Each page uses `id` attributes on elements that will be populated by
`GET /api/*` endpoints or WebSocket push events once the daemon backend
(REA-85) is wired. Until then, elements contain realistic mock values.

### dashboard.html

| Element ID | Content | Source |
|------------|---------|--------|
| `#daemon-version` | Version string (e.g. `v0.7.2`) | `GET /api/health` → `.version` |
| `#queue-depth` | Integer (e.g. `3`) | `GET /api/queue` → `.length` |
| `#passes-today` | Integer (e.g. `12`) | `GET /api/health` → `.passes_completed` |
| `#recent-passes` | `<tbody>` rows | `GET /api/passes?limit=5` |
| `#pipeline-stages` | `<tbody>` rows | `GET /api/health` → `.stages` |

### issues.html

| Element ID | Content | Source |
|------------|---------|--------|
| `#issue-list` | `<tbody>` rows | `GET /api/issues?label=agent-ready` |

Issue rows carry: `REA-NN` key, title, priority badge, status badge, assignee.

### passes.html

| Element ID | Content | Source |
|------------|---------|--------|
| `#pass-history` | `<tbody>` rows | `GET /api/passes?limit=20` |

Pass rows carry: pass number, role (build/review), issue key, outcome badge,
duration, UTC timestamp.

### plugins.html

| Element ID | Content | Source |
|------------|---------|--------|
| `#plugin-list` | `<tbody>` rows | `GET /api/plugins` |

Plugin rows carry: plugin name, version, stage badge (dev/beta/stable),
status badge (enabled/disabled), and a toggle switch for on/off control.

### Shared endpoints (REA-89)

| Endpoint | Content-Type | Source issue |
|----------|-------------|-------------|
| `GET /health` | `application/json` | REA-89 AC-5 |
| `GET /metrics` | `text/plain` (Prometheus) | REA-127 |

## Theme

`static/style.css` defines the shared dark theme with CSS custom properties
on `:root`. Layout uses `#topnav` (sticky top bar), `#shell` (sidebar +
content flex), `#sidebar` (fixed 240px left), `#content` (flex-grow main
area). Components: `.card`, `.stat`/`.stat-grid`, tables, `.badge--*`
variants, `.toggle` switch, `.indicator`, `code`/`.pre-block`. Responsive
breakpoint at 768px: nav links hide, hamburger appears, sidebar slides
off-screen.

## JavaScript

`static/app.js` provides minimal vanilla JS:
- Active-link highlighting in top nav and sidebar (reads `location.pathname`)
- Mobile sidebar toggle via `#menu-toggle` → `#sidebar.open`
- Auto-close sidebar on link click when sidebar is open

## Serving

In production the embedded `WebUIServer` (from `loop/webui.py`) serves both
static files (from `webui/static/`) and templates (from `webui/templates/`).
For local development, open any `.html` file directly — no server needed.