Static assets served by the Web UI at `/static/*`.

- `style.css` — Dark theme for all admin pages (dashboard, issues, passes,
  plugins) and the daemon status page. Uses CSS custom properties on `:root`
  so the entire color palette is centralized. Responsive layout breakpoint
  at 768px: sidebar collapses, nav links compress, grid goes single-column.