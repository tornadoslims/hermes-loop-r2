# Hermes Loop r2 — Web UI

Dark-themed static HTML/CSS scaffold for the r2 admin interface.
All pages work standalone (file:// URL, no build step).

## Architecture

```
webui/
├── static/
│   └── style.css        # Shared dark theme (CSS custom properties)
├── dashboard.html       # Pipeline overview (stats, recent passes, logs)
├── issues.html          # Issue queue and blocked issues
├── passes.html          # Build + review pass history with activity log
├── plugins.html         # Plugin inventory, config, health
└── README.md            # This file
```

## Design Decisions

- **No build tooling** — plain HTML/CSS/vanilla JS only.
- **CSS custom properties** (`:root`) define every color, spacing, and radius
  so back-end templates can reuse the same tokens.
- **CSS Grid layout** — top nav (sticky) + left sidebar + main content area.
- **Responsive** — single-column layout at ≤768px; inline nav links, no
  overlapping elements.
- **`--font-sans` / `--font-mono`** defined once; change them in
  `style.css` and every page picks them up.

## Data-Binding Points

Each page uses `id` attributes on elements that will be filled by API data.
Back-end implementors should target these selectors.

### dashboard.html

| Selector                  | Data Source          | Notes                           |
|---------------------------|----------------------|---------------------------------|
| `#queue-depth`            | `GET /api/queue`     | Queue count (number)            |
| `#active-builds`          | `GET /api/status`    | Active build worker count       |
| `#pass-rate`              | `GET /api/stats`     | 24h pass-rate percentage        |
| `#cycle-time`             | `GET /api/stats`     | Avg cycle time in minutes       |
| `#recent-passes-count`    | `GET /api/passes`    | Replace "Last 24 hours" text    |
| `#recent-passes-tbody`    | `GET /api/passes`    | Replace `<tr>` rows             |
| `#pipeline-state`         | `GET /api/health`    | Replace "State: running" text   |

### issues.html

| Selector              | Data Source          | Notes                            |
|-----------------------|----------------------|----------------------------------|
| `#open-count`         | `GET /api/issues`    | Open count                       |
| `#in-progress-count`  | `GET /api/issues`    | In-progress count                |
| `#in-review-count`    | `GET /api/issues`    | In-review count                  |
| `#done-count`         | `GET /api/issues`    | Done in last 24h                 |
| `#queue-total`        | `GET /api/issues`    | Replace "8 issues total"         |
| `#issues-tbody`       | `GET /api/issues`    | Replace `<tr>` rows              |
| `#blocked-count`      | `GET /api/issues`    | Blocked count                    |
| `#blocked-tbody`      | `GET /api/issues`    | Replace blocked `<tr>` rows      |
| `#sidebar-open-count` | `GET /api/issues`    | Open count in sidebar badge      |

### passes.html

| Selector                | Data Source          | Notes                            |
|-------------------------|----------------------|----------------------------------|
| `#total-passes`         | `GET /api/passes`    | 24h total pass count             |
| `#success-rate`         | `GET /api/passes`    | Success percentage               |
| `#abort-count`          | `GET /api/passes`    | 24h abort count                  |
| `#avg-build-time`       | `GET /api/passes`    | Avg build duration (min)         |
| `#build-passes-count`   | `GET /api/passes`    | Replace "Last 14 passes"         |
| `#build-passes-tbody`   | `GET /api/passes`    | Replace `<tr>` rows              |
| `#review-passes-count`  | `GET /api/passes`    | Replace "Last 9 passes"          |
| `#review-passes-tbody`  | `GET /api/passes`    | Replace `<tr>` rows              |
| `#log-entries`          | `GET /api/log`       | Replace "Live stream"            |

### plugins.html

| Selector               | Data Source          | Notes                            |
|------------------------|----------------------|----------------------------------|
| `#plugin-grid`         | `GET /api/plugins`   | Replace plugin cards             |
| `#config-source`       | `GET /api/config`    | Replace "Source: loop.toml"      |
| `#sidebar-plugin-count`| `GET /api/plugins`   | Active plugin count              |
| `#health-status`       | `GET /api/health`    | Replace "All core plugins ..."   |
| `#health-tbody`        | `GET /api/health`    | Replace plugin health rows       |

## Cross-Page IDs

These selector IDs appear on multiple pages (same name, same semantics):

| ID                         | Pages                        | Data Source       |
|----------------------------|------------------------------|-------------------|
| `#sidebar-plugin-count`    | dashboard, issues, passes, plugins | `GET /api/plugins` |
| `#sidebar-open-count`      | dashboard, issues            | `GET /api/issues`  |

## Adding New Pages

1. Copy an existing `.html` page as a starting point.
2. The `<nav class="top-nav">` and `<aside class="sidebar">` markup should
   stay identical across all pages for consistent navigation.
3. Set `class="active"` on the matching nav link and sidebar link.
4. Add your data-binding selectors to this README under a new section.

## Known Limitations

- **No live refresh** — pages are static. A future issue (e.g. REA-89 for
  `/health` endpoint) can add vanilla JS polling via `setInterval` +
  `fetch` without introducing build tooling.
- **No auth** — authentication UI is explicitly excluded (NG-3). The
  daemon serving these files should handle auth at the HTTP layer.