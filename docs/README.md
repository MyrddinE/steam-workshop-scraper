# Documentation

These documents describe how the project works as it exists today. They are the published
reference for the code in `src/`.

## Orientation

| Document | Covers |
|---|---|
| [architecture.md](architecture.md) | The system at a glance: components, data sources, and how a request flows from discovery to display. |

## Data

| Document | Covers |
|---|---|
| [data-model.md](data-model.md) | Every table and column, what owns it, and what it means. |
| [timestamps.md](timestamps.md) | The three clocks (Steam's, ours, and stored version keys) and the write rules for each column. |
| [schema-migrations.md](schema-migrations.md) | The `PRAGMA user_version` migration chain and the current schema version. |
| [live-data-profile.md](live-data-profile.md) | Measured contents of the production database: scale, backlog, anomalies, and text shapes. |

## Pipeline

| Document | Covers |
|---|---|
| [data-pipeline.md](data-pipeline.md) | How an item moves through discovery, API fetch, web scraping, image download, translation, and display. |
| [failure-capture.md](failure-capture.md) | What happens to input the scraper cannot handle: captured artefacts, their bounds, and promotion to regression tests. |

## Interfaces

| Document | Covers |
|---|---|
| [tui.md](tui.md) | The Terminal UI: screens, search builder, list/detail views, and state persistence. |
| [web-ui.md](web-ui.md) | The Flask/Waitress web UI: layout, endpoints, infinite scroll, polling, and the subscribe bridge. |
| [search-filter.md](search-filter.md) | The shared search/filter semantics used by both UIs. |
| [threading.md](threading.md) | Threads, shared state, column ownership, and startup/shutdown ordering. |

## Operations

| Document | Covers |
|---|---|
| [config-security.md](config-security.md) | Every configuration key, where it is read, and the security boundaries. |
| [cross-platform.md](cross-platform.md) | Linux/Windows differences in process management, encoding, and file paths. |

## Caveats

| Document | Covers |
|---|---|
| [code-issues.md](code-issues.md) | Known defects, each re-checked against the source, with status and priority. |
