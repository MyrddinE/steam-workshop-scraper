# Configuration & Security

The configuration system uses a YAML file with environment variable overrides for secrets. This document covers every config key, its purpose, where it's read, and security considerations.

---

## Configuration Loading

### `load_config(path)` (config)

Reads a YAML file and applies environment variable overrides. The only two env vars supported:
- `STEAM_API_KEY` → `config["api"]["key"]`
- `OPENAI_API_KEY` → `config["openai"]["api_key"]`

If the config file doesn't exist, `load_config` raises `FileNotFoundError`. Callers handle this by falling back to defaults.

A file that exists but cannot be parsed is a different case: it is a misconfiguration, not an absent
default, so `load_config` raises `ConfigError` — distinct from `FileNotFoundError` — naming the
file and the position, and every entry point reports it and exits non-zero rather than starting
with defaults.
Falling back to defaults there would silently discard whatever the operator intended. Windows paths
are the common way to trip this — inside a double-quoted YAML scalar a backslash starts an escape, so
`"D:\Temp\dsh"` is a parse error and `"D:\temp"` is worse, parsing without complaint to `D:<TAB>emp`.
Single quotes leave backslashes alone (`'D:\Temp\dsh'`), as does leaving the value unquoted.

### `save_config(path, config)` (config)

Deep-merges the in-memory config into the disk file, preserving keys not present in the in-memory dict. Strips environment-derived secrets (API keys that match env var values) before writing to avoid persisting credentials to disk. If the config file doesn't exist, `save_config` returns without writing (a no-op used by the TUI's port persistence).

---

## Config Keys Reference

### `api`

| Key | Type | Default | Purpose |
|---|---|---|---|
| `api.key` | string | None | Steam Web API key. Required for item discovery and detail fetching. Overridden by `STEAM_API_KEY` env var. |

### `openai`

| Key | Type | Default | Purpose |
|---|---|---|---|
| `openai.api_key` | string | None | OpenAI-compatible API key for translation. Overridden by `OPENAI_API_KEY` env var. |
| `openai.model` | string | `"gpt-4o-mini"` | Model name passed to the OpenAI client. |
| `openai.endpoint` | string | `"https://api.openai.com/v1"` | API endpoint URL (supports alternative providers like x.ai). |

### `daemon`

| Key | Type | Default | Purpose |
|---|---|---|---|
| `daemon.target_appids` | list[int] | (required) | Steam AppIDs to scrape. The daemon refuses to start without this. |
| `daemon.batch_size` | int | 10 | Items to process per `process_batch` iteration. |
| `daemon.api_delay_seconds` | float | 1.5 | Seconds to wait between Steam API calls. Dynamically adjusted by the success/failure streak mechanism. |
| `daemon.web_delay_seconds` | float | 5.0 | Seconds to wait between web scrape requests. Dynamically adjusted. |
| `daemon.image_delay_seconds` | float | 2.0 | Seconds to wait between image downloads. Dynamically adjusted. |
| `daemon.item_staleness_days` | int | 30 | Days before a successfully-scraped item is considered stale and re-scraped. |
| `daemon.user_staleness_days` | int | 90 | Days before a user profile is re-fetched from Steam. |
| `daemon.outbox_dir` | string | None | Directory a separate machine pulls artefacts from. Enables [failure capture](failure-capture.md) on its own, and database backups together with `backup_interval_seconds` below. The daemon and the puller must not run as the same OS user unless the outbox is writable by both. `daemon.backup_dir` is an accepted legacy alias. |
| `daemon.backup_interval_seconds` | float | 0 (off) | Seconds between verified database snapshots into `<outbox_dir>/db/`. Backups are **off** unless this is positive *and* `outbox_dir` is set, so enabling backups on the live instance is a deliberate switch. |
| `daemon.capture_web_scrapes` | bool | false | **Debugging switch.** Save *every* web scrape — successes as well as failures — to `<outbox_dir>/scrapes`, body kept whole. The failure capture only ever holds misses, so it cannot show what a working, signed-in page looks like; this exists to identify that markup, which is a more direct signal that the session is still good than a failed scrape is. Deliberately unbounded and unstripped: it is on for a session or two, and thinning a sample before anyone has looked at it only means collecting the evidence twice. Each record carries an `auth_markers` map of candidate marker strings and the `g_steamID` value, recorded rather than interpreted. Turn it off when done. |

### `web`

| Key | Type | Default | Purpose |
|---|---|---|---|
| `web.port` | int | None | Web server port. If unset, the TUI picks a random free port and persists it. |
| `web.host` | string | `"0.0.0.0"` | Web server bind address. |

### `database`

| Key | Type | Default | Purpose |
|---|---|---|---|
| `database.path` | string | `"workshop.db"` | SQLite database file path. |

### `logging`

| Key | Type | Default | Purpose |
|---|---|---|---|
| `logging.level` | string | `"INFO"` | Log level (DEBUG, INFO, WARNING, ERROR). |
| `logging.file` | string | None | Log file path. If set, logs are written to this file in addition to console (daemon) or instead of console (TUI). |

### `session`

| Key | Type | Default | Purpose |
|---|---|---|---|
| `session.id` | string | None | Steam `sessionid` cookie value. Used by the server-side subscribe endpoint as the CSRF token. Also used by `web_scraper._build_workshop_cookies`. Pushed from the userscript via `/api/sessionid`. |
| `session.login_secure` | string or list | None | Steam `steamLoginSecure` cookie. Can be a raw string or a YAML list of its 3 pipe-separated components. When a list, joined with `%7C%7C` at runtime. Required for server-side subscribe (web UI subscribe uses the userscript bridge which doesn't need this). |
| `session.read_firefox_cookies` | bool | false | Take `steamLoginSecure` **and** `sessionid` from the local Firefox profile, in preference to the values above. The store holds both — `sessionid` is not HttpOnly, which is why the userscript could always supply it — and reading them together means the credential and the CSRF token cannot come from different sessions. Profiles are discovered, never named. Off by default. Presence of a configured value is **not** evidence of a working session: a stale `login_secure` shadowed a good browser cookie and made every scrape anonymous, so the configured value is only a fallback and the response itself decides whether authentication is still good. |

---

## Security Boundaries

### Credential Protection

- API keys (`STEAM_API_KEY`, `OPENAI_API_KEY`) are loaded from environment variables or the config file. The config file should not contain these keys.
- `save_config` strips any config value that matches an environment variable before writing — this prevents accidentally persisting env-derived secrets.
- The `session.id` and `session.login_secure` are stored in the config file. These are Steam session cookies. `sessionid` is a CSRF token; `steamLoginSecure` is what authenticates the session, and it is what the subscribe call and the workshop scrape both send. Treat both as secrets. `steamLoginSecure` expires, and the userscript refreshes it by pushing to `/api/sessionid`, which persists it so the daemon picks it up without a restart.

### Session Cookie Handling

- The `sessionid` cookie (not HttpOnly) is captured by the userscript via `document.cookie` on `steamcommunity.com` and pushed to the server.
- The `steamLoginSecure` cookie (HttpOnly) cannot be read by JavaScript. The server-side subscribe endpoint reads it from the config file (manually configured by the user). The web UI subscribe route avoids this entirely by using the userscript bridge.
- No Steam credentials are ever sent to the browser. The `/api/subscribe` endpoint runs entirely server-side.

### Web Server

- The embedded Flask/Waitress server binds to all interfaces (`0.0.0.0`) by default. In production behind NAT or a firewall, this is accessible only within the local network. The randomly-assigned port provides minimal obscurity but not security.
- No authentication is implemented on the web server. All API endpoints are open to anyone with network access.

### Userscript Trust Model

The subscribe userscript runs with `@grant GM_xmlhttpRequest` and `@grant GM_setValue/GM_getValue`. The sessionid stored in `GM_setValue` is accessible only to this userscript (GM storage is namespaced). The userscript is served via the dynamic `/userscript/` endpoint, which injects `@include` lines for the server's host. A malicious server could inject arbitrary `@include` lines, but this requires compromising the server itself.
