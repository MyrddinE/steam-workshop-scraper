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
| `openai.batch` | int | 20 | Ceiling on fields per translation request. Bounds how many rows one bad reply can cost. |
| `openai.batch_char_cap` | int | 4000 | Ceiling on the summed source length of one request. The first field is taken before the cap is checked; every later field is checked before it is taken, so only a single-field request may exceed it. |
| `openai.temperature` | float | 0.0 | Sampling temperature. Translation is not a creative task, so sampling only adds variance; the value is clamped to 0–2. |

There is deliberately no `max_tokens` key: a translation is never cut to a token
budget, because a truncated translation is a corrupt one. The size ceiling is
`openai.batch_char_cap` instead, which bounds what is *asked for* rather than
silently truncating what comes back.

### `daemon`

| Key | Type | Default | Purpose |
|---|---|---|---|
| `daemon.target_appids` | list[int] | (required) | Steam AppIDs to scrape. The daemon refuses to start without this. |
| `daemon.batch_size` | int | 10 | Items to process per `process_batch` iteration. |
| `daemon.api_delay_seconds` | float | 1.5 | A literal pause added between Steam API requests, not a target rate. Adjusted per **request** (batched item results do not move it): decays 10 ms on each healthy request and doubles on each refusal, so it converges just under the sustainable rate. |
| `daemon.web_delay_seconds` | float | 6.0 | Seconds to wait between web scrape requests. Dynamically adjusted; the decay rule floors it at 6.0 s, so the default is set to the floor rather than below it. |
| `daemon.image_delay_seconds` | float | 2.0 | Seconds to wait between image downloads. Dynamically adjusted. |
| `daemon.item_staleness_days` | int | 30 | Days before a successfully-scraped item is considered stale and re-scraped. |
| `daemon.user_staleness_days` | int | 90 | Days before a user profile is re-fetched from Steam. |
| `daemon.outbox_dir` | string | None | Directory a separate machine pulls artefacts from. Enables [failure capture](failure-capture.md) on its own, and database backups together with `backup_interval_seconds` below. The daemon and the puller must not run as the same OS user unless the outbox is writable by both. `daemon.backup_dir` is an accepted legacy alias. |
| `daemon.backup_interval_seconds` | float | 0 (off) | Seconds between verified database snapshots into `<outbox_dir>/db/`. Backups are **off** unless this is positive *and* `outbox_dir` is set, so enabling backups on the live instance is a deliberate switch. |
| `daemon.capture_web_downloads` | bool | false | **Debugging switch.** Save *every* Steam community web pull — the item page, the subscriptions page, and the server-side subscribe `POST` — to `<outbox_dir>/web_downloads`, with both sides of the exchange: request method, URL, headers, cookies and form data, and response status, final URL, headers and body, the body kept whole. The failure capture only ever holds misses, so it cannot show what a working, signed-in exchange looks like; this is the instrument for finishing the subscribe path against real traffic. **No credential value is ever written:** cookie values, the `sessionid` form field and `Cookie`/`Set-Cookie`/`Authorization` header values become `***` (their *names* stay — knowing which cookie was sent is the diagnostic point), and those literal values are scrubbed from everything written, including the body file. Deliberately unbounded and unstripped: it is on for a session or two, and thinning a sample before anyone has looked at it only means collecting the evidence twice. Does **not** cover the Steam Web API (`src/steam_api.py`, a different surface with its own failure capture) or image downloads (`daemon.capture_image_downloads`). The legacy name `daemon.capture_web_scrapes` is still honoured, with a deprecation warning. Turn it off when done. |
| `daemon.capture_image_downloads` | bool | false | **Debugging switch**, separate from the web one. Save *every* image download — successes as well as failures — to `<outbox_dir>/image_downloads`, as metadata and headers only: status, URL, content type, bytes written, and the path of the image on disk. The image bytes are never copied into the outbox; the downloaded file is the artefact. Image *failures* need neither this switch nor `capture_web_downloads` — an `outbox_dir` alone captures them. Turn it off when done. |

### `web`

| Key | Type | Default | Purpose |
|---|---|---|---|
| `web.port` | int | None | Web server port. If unset, the TUI picks a random free port and persists it. |
| `web.host` | string | `"0.0.0.0"` | Web server bind address. |

### `database`

| Key | Type | Default | Purpose |
|---|---|---|---|
| `database.path` | string | `"workshop.db"` | SQLite database file path. |

### `steam`

Windows only: how the downloaded-item marker and the "open folder" action find
Steam's workshop content. On any other platform, or with no Steam install to
read, the feature is off and every item stays without a `downloaded` marker.

| Key | Type | Default | Purpose |
|---|---|---|---|
| `steam.workshop_content_dirs` | list[string] | `[]` | Extra workshop *content* roots (`<library>\steamapps\workshop\content`) to check **in addition to** the ones discovered from Steam's install. Discovery reads `SteamPath` from `HKCU\Software\Valve\Steam` and parses that install's `libraryfolders.vdf` (both `<steam>/steamapps/` and the older `<steam>/config/`), so this key is only needed for a library those files do not name — a network drive, a moved folder, or a Steam install the app cannot read. An entry is the folder that holds `<consumer_appid>\<workshop_id>\`, not the library root. A bare string is accepted as a one-entry list. |

### `logging`

| Key | Type | Default | Purpose |
|---|---|---|---|
| `logging.level` | string | `"INFO"` | Log level (DEBUG, INFO, WARNING, ERROR). |
| `logging.file` | string | None | Log file path. If set, logs are written to this file in addition to console (daemon) or instead of console (TUI). |

### `session`

| Key | Type | Default | Purpose |
|---|---|---|---|
| `session.id` | string | None | Steam `sessionid` cookie value, used by `web_scraper._build_workshop_cookies`. The scrape path reads the CSRF token from that cookie set, so this configured value (like the pushed `_pushed_sessionid` global) is a fallback when the set carries none. The server-side subscribe endpoint does **not** take its token from here: it uses the `g_sessionID` of the item page the attempt reads, because `sessionid` is a session cookie Firefox never writes to `cookies.sqlite` — this value and the pushed global are only that path's fallback for a page with no token. Pushed from the userscript via `/api/sessionid`. |
| `session.login_secure` | string or list | None | Steam `steamLoginSecure` cookie. Can be a raw string or a YAML list of its 3 pipe-separated components. When a list, joined with `%7C%7C` at runtime. Required for a server-side subscribe unless `session.read_firefox_cookies` supplies it from the browser profile; the web UI's subscribe route uses the userscript bridge, which subscribes in the browser and needs neither. The debug captures never write the value: `daemon.capture_web_downloads` elides every cookie value to `***` (see that row below). |
| `session.read_firefox_cookies` | bool | false | Send the local Firefox profile's **whole** `steamcommunity.com` cookie set on Workshop scrapes, in preference to the configured values. The store holds `steamLoginSecure` and, for scrapes, `sessionid` — `sessionid` is not HttpOnly, which is why the userscript could always supply it — and reading them together for a scrape means the credential and the CSRF token cannot come from different sessions. (The profile read can never carry the *current* `sessionid` — it is a session cookie Firefox keeps in memory and never persists — so the subscribe path takes the page's own `g_sessionID` instead, with this read only a fallback.) This is what a real navigation sends: the browser's ten cookies for the domain, not two hand-picked names, and the scrape request is deliberately shaped to match a browser. Profiles are discovered, never named. The read is remembered for the process, but not past its own credential's expiry: while the remembered `steamLoginSecure` is live — or states no readable expiry — the set is served from the cache, and once its token says it has expired the profile is read again, so a daemon running for weeks picks up the cookie Steam reissues daily. An empty read is never cached. Off by default; when off, only the configured `sessionid`/`login_secure` pair is sent. Presence of a configured value is **not** evidence of a working session: a stale `login_secure` shadowed a good browser cookie and made every scrape anonymous, so the configured value is only a fallback and the response itself decides whether authentication is still good. |

---

## Security Boundaries

### Credential Protection

- API keys (`STEAM_API_KEY`, `OPENAI_API_KEY`) are loaded from environment variables or the config file. The config file should not contain these keys.
- `save_config` strips any config value that matches an environment variable before writing — this prevents accidentally persisting env-derived secrets.
- The `session.id` and `session.login_secure` are stored in the config file. These are Steam session cookies. `sessionid` is a CSRF token; `steamLoginSecure` is what authenticates the session, and it is what the subscribe call and the workshop scrape both send. Treat both as secrets. `steamLoginSecure` expires — *measured live* on 2026-09-17, the token carries `exp - iat` of 24.1 hours, so Steam reissues it about daily and an untouched profile goes stale within a day — and the userscript refreshes it by pushing to `/api/sessionid`, which persists it so the daemon picks it up without a restart. The value states its own expiry and `src/session_cookie.py` reads it, so code can refuse a dead cookie without asking Steam; when one is recorded, the web UI shows a warning with a link to sign in again ([web-ui.md](web-ui.md#the-session-warning)). When `session.read_firefox_cookies` is on, the Workshop scrape and the server-side subscribe call both send the profile's whole `steamcommunity.com` cookie set instead, which includes these two plus the browser's own non-credential state (`timezoneOffset`, `steamCountry`, `browserid`, and so on). That is deliberate — it is what a real navigation sends, and the request is shaped to match one — and none of the extra cookies is a credential.

### Crash Dumps

An unhandled traceback is written to
`<outbox_dir>/crashes/<stamp>-<process>-error<N>.txt` by `src/crash.py`, installed
from all three entry points (`src/tui.py`, `src/web_runner.py`,
`src/daemon_runner.py`). Because the traceback includes each frame's **locals**,
this is the one outbox artefact that can hold whatever the process happened to be
holding at crash time, so it is reduced three ways before it is written:

* a mapping entry — or a local variable — whose name contains `cookie`, `token`,
  `secret`, `password`, `passwd`, `credential`, `login` or `sessionid`, or ends
  in `key`, has its value replaced with `***`;
* every literal value the process knows is scrubbed from the finished text,
  longest first: the current cookie set, `session.id` and `session.login_secure`
  in both accepted forms, `api.key` **and** `STEAM_API_KEY`, `openai.api_key`
  **and** `OPENAI_API_KEY` (`load_config` strips an env-derived key from the
  config before saving, so the config alone is not enough), plus anything
  registered at runtime through `crash.register_secret` — the pushed `sessionid`
  and a refreshed `steamLoginSecure`;
* values are truncated and the whole file is capped at 256 KB, with the caps
  stated in the header.

This is **not** a complete barrier. It is key-name based and known-value based,
so a credential the process never registered, the config does not hold, and no
key name describes would still be written. The dump therefore goes only to the
owner's own outbox — it is collected by their sync and is never published — and
[docs/failure-capture.md](failure-capture.md) records the same residual risk.
With no outbox configured the dump falls back beside the configured log file,
else into the working directory, and the path is printed to the console.

### Session Cookie Handling

- The `sessionid` cookie (not HttpOnly) is captured by the userscript via `document.cookie` on `steamcommunity.com` and pushed to the server.
- The `steamLoginSecure` cookie (HttpOnly) cannot be read by JavaScript. The server-side subscribe endpoint reads it through `web_scraper._build_workshop_cookies` — the browser profile when `session.read_firefox_cookies` is on, otherwise the configured value — and refuses before making a request when neither source has one. The web UI's subscribe route avoids the credential entirely by using the userscript bridge.
- For a Workshop scrape, `session.read_firefox_cookies` sends **every** `steamcommunity.com` cookie in the profile, not only the two above. A real Firefox navigation sends all of them, and the scrape request is deliberately shaped to match a real navigation; the additional names are benign browser state, not credentials. With the setting off, only the configured `sessionid`/`login_secure` pair is sent.
- The profile read is cached for the life of the process, but the cache does not outlive the credential it holds. `firefox_cookies.steam_community_cookies` serves the remembered set while the `steamLoginSecure` token is live or states no readable expiry — unknown is not treated as expired, so a value that cannot be decoded is still sent — and re-reads the whole profile once the token says it has expired. That re-read is a local file copy, not a request, and it is what lets a daemon that has been up for days stop sending a credential Steam has already killed; an empty read was never cached and still is not.
- No Steam credentials are ever sent to the browser. The `/api/subscribe` endpoint runs entirely server-side.

### Web Server

- The embedded Flask/Waitress server binds to all interfaces (`0.0.0.0`) by default. In production behind NAT or a firewall, this is accessible only within the local network. The randomly-assigned port provides minimal obscurity but not security.
- No authentication is implemented on the web server. All API endpoints are open to anyone with network access.

### Userscript Trust Model

The subscribe userscript runs with `@grant GM_xmlhttpRequest` and `@grant GM_setValue/GM_getValue`. The sessionid stored in `GM_setValue` is accessible only to this userscript (GM storage is namespaced). The userscript is served via the dynamic `/userscript/` endpoint, which injects `@include` lines for the server's host. A malicious server could inject arbitrary `@include` lines, but this requires compromising the server itself.
