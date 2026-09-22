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
| `openai.batch_char_cap` | int | 4000 | Ceiling on the estimated cost of one request: each field's source text plus a fixed 50-character allowance for the boundary scaffolding (the phrase, the entity id, the field label and the newline; `PER_FIELD_OVERHEAD_CHARS` in `src/translator.py`). The first field is taken before the cap is checked; every later field is checked before it is taken, so only a single-field request may exceed it. There is no item ceiling, so the number of fields follows from their lengths. |
| `openai.temperature` | float | 0.0 | Sampling temperature. Translation is not a creative task, so sampling only adds variance; the value is clamped to 0–2. |

`openai.batch_items` is **no longer read** — the request is bounded by
`openai.batch_char_cap` alone — and neither is its legacy spelling `openai.batch`.
A config that still carries either key gets one warning naming the key, and needs
no other change.

There is deliberately no `max_tokens` key: a translation is never cut to a token
budget, because a truncated translation is a corrupt one. The size ceiling is
`openai.batch_char_cap` instead, which bounds what is *asked for* rather than
silently truncating what comes back.

### `daemon`

| Key | Type | Default | Purpose |
|---|---|---|---|
| `daemon.target_appids` | list[int] | (required) | Steam AppIDs to scrape. The daemon refuses to start without this. |
| `daemon.api_batch_size` | int | 10 | Items to process per `process_batch` iteration. `daemon.batch_size` is **no longer read**: a config that still carries it gets one warning naming the key, and the value must be moved to the current spelling, which is the only one that influences anything. |
| `daemon.item_staleness_days` | int | 30 | Days before a successfully-scraped item is considered stale and re-scraped. |
| `daemon.creator_staleness_days` | int | 90 | Days before a creator profile is re-fetched from Steam. `daemon.user_staleness_days` is **no longer read**: a config that still carries it gets one warning naming the key, and the value must be moved to the current spelling, which is the only one that influences anything. |
| `daemon.cutoffs_cache_seconds` | number | 86400 | How old a persisted percentile-cutoff entry may be before `POST /api/cutoffs` calls it **stale**, kept in `.cutoffs_cache.json` beside the database (atomic write, the same state-file discipline as `.daemon_state.yaml`). **Read by the web server**, from the `daemon` section like the capture switches; the daemon itself does not use it. A stale entry is **not dropped**: it is still served immediately (stale highlighting beats no highlighting), and the pull that got it starts one background regeneration so the next pull finds fresh values; a failed regeneration leaves the stale entry to be served and retried again. `0` disables caching, so every request recomputes synchronously. The entry is keyed by the query and the database's `user_version`, so a changed query or schema change misses regardless. See [web-ui.md](web-ui.md#api-cutoffs--post). |
| `daemon.outbox_dir` | string | None | Directory a separate machine pulls artefacts from. Enables [failure capture](failure-capture.md) on its own, and database backups together with `backup_interval_seconds` below. The daemon and the puller must not run as the same OS user unless the outbox is writable by both. The **debug** capture trees (`web_downloads/`, `image_downloads/`, `web_ui_trace/`, and the legacy `scrapes/`) are pruned by housekeeping once a file is seven days old; `failures/` and `crashes/` are never pruned by age, and the pull tool moves a file (removing it and its manifest entry) once it has been fetched for review. A crash dump written before this key was known lands in the application folder first and is adopted into `crashes/` at the next successful start ([failure-capture.md](failure-capture.md#crash-dumps)). `daemon.backup_dir` is a **still-honoured** legacy alias: it supplies this value when the current key is absent, and the daemon, the web server and the crash writer all read it through one accessor that logs one deprecation warning per process. Rename it to `daemon.outbox_dir`; the alias is retained until the log shows it unused. |
| `daemon.backup_interval_seconds` | float | 0 (off) | Seconds between verified database snapshots into `<outbox_dir>/db/`. Backups are **off** unless this is positive *and* `outbox_dir` is set, so enabling backups on the live instance is a deliberate switch. A snapshot is written as a complete second copy in that directory before it is published, so one run needs room for roughly two copies of the database: if the volume demonstrably has less than the source size plus a fixed headroom free, the run is refused up front and the previous snapshot is left in place ([failure-capture.md](failure-capture.md#retention)). An unavailable free-space reading is not evidence of no room, and does not refuse. |
| `daemon.capture_web_downloads` | bool | false | **Debugging switch.** Save *every* Steam community web pull — the item page, the subscriptions page, and the server-side subscribe `POST` — to `<outbox_dir>/web_downloads`, with both sides of the exchange: request method, URL, headers, cookies and form data, and response status, final URL, headers and body, the body kept whole. The failure capture only ever holds misses, so it cannot show what a working, signed-in exchange looks like; this is the instrument for finishing the subscribe path against real traffic. **No credential value is ever written:** cookie values, the `sessionid` form field and `Cookie`/`Set-Cookie`/`Authorization` header values become `***` (their *names* stay — knowing which cookie was sent is the diagnostic point), and those literal values are scrubbed from everything written, including the body file. Deliberately not thinned while it is on, and the body is kept whole: it is on for a session or two, and thinning a sample before anyone has looked at it only means collecting the evidence twice. What bounds the directory is age rather than volume — housekeeping prunes a debug capture after seven days ([failure-capture.md](failure-capture.md#retention)). Does **not** cover the Steam Web API (`src/steam_api.py`, a different surface with its own failure capture) or image downloads (`daemon.capture_image_downloads`). `daemon.capture_web_scrapes` is **no longer read**: a config that still carries it gets one warning naming the key and captures nothing, so the value must be moved to the current spelling. Turn it off when done. |
| `daemon.capture_image_downloads` | bool | false | **Debugging switch**, separate from the web one. Save *every* image download — successes as well as failures — to `<outbox_dir>/image_downloads`, as metadata and headers only: status, URL, content type, bytes written, and the path of the image on disk. The image bytes are never copied into the outbox; the downloaded file is the artefact. Image *failures* need neither this switch nor `capture_web_downloads` — an `outbox_dir` alone captures them. The directory is a debug tree, so it is pruned by age like the web captures ([failure-capture.md](failure-capture.md#retention)). Turn it off when done. |
| `daemon.capture_web_ui_trace` | bool | false | **Debugging switch.** Save the page's own behaviour — the one thing a static reading of `templates/index.html` cannot show — to `<outbox_dir>/web_ui_trace`, one JSON file per posted batch, registered with the puller as `kind: "ui_trace"`. Each record holds a session header (load time, viewport, grid `clientHeight`/`scrollTop`, build version) and an ordered timeline of: the user's actions (keydown with whether a handler consumed it, click, throttled scroll with the grid's `scrollTop`/`scrollHeight`, sort and overlay changes, a creator jump, a pane open); every `fetch` they cause, recorded by one wrapper around `fetch` so a call nobody instrumented still appears (method, path, a summary of the body — filter count, id count, offset, never a whole payload — start, duration, status, and the response item count where cheap); and the internal transitions that explain them (`doSearch` entry and whether the `loading` guard dropped it, `_observeNextBatch`'s branch and geometry, the `IntersectionObserver` callback and whether its entry matched the armed cell, each list/item poll tick). Every record carries `loads_since_scroll`, so "N loads with no scroll between them" is one line rather than an inference. **Bounded three ways** because this tree is far denser than a web capture: `UI_TRACE_MAX_EVENTS_PER_BATCH` (200) events per batch, oldest dropped first; `UI_TRACE_MAX_RECORD_BYTES` (256 KB) per record; and `UI_TRACE_MAX_FILES_PER_SESSION` (200) files per page session, after which the crossing record names the truncation and the route refuses the rest while the page stops buffering — every truncation is recorded, never silent — and the page's own buffer is a ring buffer. The page is told the switch through the template, so **off it installs nothing at all** (no `fetch` wrapper, no listener, no buffer). The record is scrubbed with the same credential values as a crash dump even though the page never holds a cookie. It is an instrument, not evidence: housekeeping prunes the whole tree once a file is seven days old ([failure-capture.md](failure-capture.md#retention)), and the tree is never a substitute for the failure and crash evidence. **The same switch also gates the sort diagnostic**: with it on, `GET /api/search_diagnostic` answers a read-only report on the live database's sort path (each expected index's actual definition against the expected one — `present`/`missing`/`wrong_definition` — per-sort `EXPLAIN QUERY PLAN` with a derived `uses_index` boolean, score coverage, `sqlite_stat1`, first/deep page timings) and `init_webserver` logs the same summary once; with it off the route is inert and no startup query runs. It has no tree of its own and writes nothing. Turn it off when done. |

#### Pacing delays are state, not config

The three inter-request pacing delays are **not configuration**. Each is daemon
state, kept in `.daemon_state.yaml` beside the database — the same
restart-surviving store the translator's backoff uses (`src/daemon_state.py`) —
and written as the delay moves, bounded by `pacing.PERSIST_STEP_SECONDS` so it is
not rewritten on every request. One section per worker:

| State section | Default | What it paces |
|---|---|---|
| `api_delay` | 1.5 s | Steam API requests, adjusted per **request** (batched item results do not move it): it decays with healthy operation and doubles on each refusal, converging just under the sustainable rate. |
| `web_delay` | 6.0 s | Web scrape requests, from the same shape and floored at 6.0 s, so the default is the floor rather than below it. |
| `image_delay` | 2.0 s | Image downloads, from the same shape and floored at 0.5 s. |

`daemon.api_delay_seconds`, `daemon.web_delay_seconds`,
`daemon.image_delay_seconds` and their original spelling
`daemon.request_delay_seconds` are **no longer read**. A config that still
carries one of them gets one warning saying the value is no longer read, and is
otherwise ignored: there is no current key to rename it to, because the value
moved to the state file rather than to another config spelling. Deleting the
retired key is the only change an operator needs to make.

**Resetting a delay by hand.** The reason these values used to live in
`config.yaml` was that an operator needs to pull a delay back down when it
over-reacts, and that stays cheap: edit or delete the worker's section in
`.daemon_state.yaml`. Deleting `web_delay` (or any of the three) returns that
worker to the default above; the next daemon start reads it. It is one section
per worker, so resetting one leaves the other two — and the translator's
`translation_backoff`, which is not part of this — alone. It needs no Python file
changed and no unusual restart, and a text editor or a YAML tool is the only
client required.

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
| `logging.file` | string | None | Log file path. If set, logs are written to this file in addition to console (daemon) or instead of console (TUI). The file grows without bound and is **never rotated automatically**: a manual **Rotate Log** control on the daemon page of both front ends renames the live file to `<same folder>/logs/<stem>-<UTC stamp>.log.gz` and leaves a fresh empty file at the original path. **There is no retention policy** — archives accumulate — because deciding when to delete the owner's log history is not this program's call. |

#### Manual rotation, and why it is manual

The log grows about 115 MB a day (measured 2026-09-18: 594 MB across 5.63 M lines), so it needs a bound. It does not get an automatic one because the owner keeps a persistent `tail` open in another window: a rotation the daemon chose on its own would disrupt that view without warning. Rotation is therefore operator-initiated only — nothing runs on a timer, on a size threshold, or at startup — and the operator accepts that pressing the button disrupts the tail they are watching. On Windows that is stronger than a disruption, and the owner accepted this too: the rename needs **every** holder to have opened the file with `FILE_SHARE_DELETE`, and an ordinary reader — including the tail — does not, so the rotation is *refused* while the tail is attached, the button says `Rotation failed: [WinError 32] …`, the log is left untouched, and the operator closes the tail, rotates and reopens it. *Measured on the production host* (Windows, CPython 3.12.10): renaming a file held open with `FILE_SHARE_READ|WRITE|DELETE` succeeds; renaming one held by an ordinary `open()` fails with `WinError 32`.

Two processes hold the file open — the daemon and the TUI — so the rotation also has to make each of them reopen it. The rotator writes the archive's name to `<log file>.generation` beside the log, and every handler built by `src/log_rotation.log_file_handler` caches that marker and reopens when it changes. A rename alone would leave a handler writing into the renamed inode, and those records would disappear into the compressed archive silently. `logging.handlers.WatchedFileHandler` was rejected because it does not reopen on Windows, which is where this runs. On Windows the handlers also open the log **with `FILE_SHARE_DELETE`**: Python's ordinary `open()` asks only for read/write sharing, so a file a handler holds cannot be renamed by another process and the rename would be refused for as long as the daemon is running (`src/log_rotation.py` opens it through the Win32 API for this reason).

Rotation awareness is an enhancement, never a precondition for logging, and the code takes that stance in two places. A marker read that fails for **any** reason — not only the ordinary `OSError` of a missing marker — degrades to "no generation recorded" and warns; the handler still opens the log. And if the rotation-aware handler cannot be built at all, `src/log_rotation.log_file_handler` installs an ordinary `logging.FileHandler` with a warning instead. Logging keeps working in both cases; what is lost is following a manual rotation, and the warning is how the operator learns it. Because both front ends build their file handler through that one factory, they cannot drift on the fallback.

The rename is done before the button's request answers, so the live path is new immediately; the gzip runs on a background thread and the daemon page shows `Rotating… (<size>)` until it finishes, then `Rotated: logs/<name> (<size>)`. A compression failure leaves the renamed file uncompressed and names it, rather than deleting the log. Concurrent presses are serialised by a lock file beside the archives, so two rotations cannot archive the same content twice.


### `session`

| Key | Type | Default | Purpose |
|---|---|---|---|
| `session.csrf_token` | string | None | Steam `sessionid` cookie value, used by `web_scraper._build_workshop_cookies`. The scrape path reads the CSRF token from that cookie set, so this configured value is a fallback when the set carries none. The server-side subscribe endpoint does **not** take its token from here: it uses the `g_sessionID` of the item page the attempt reads, because `sessionid` is a session cookie Firefox never writes to `cookies.sqlite` — this value is only that path's fallback for a page with no token. `session.id` is **no longer read**: a config that still carries it gets one warning naming the key, and the value must be moved to the current spelling, which is the only one that influences anything. |
| `session.login_secure` | string or list | None | Steam `steamLoginSecure` cookie. Can be a raw string or a YAML list of its 3 pipe-separated components. When a list, joined with `%7C%7C` at runtime. Required for a server-side subscribe unless `session.read_firefox_cookies` supplies it from the browser profile; the web UI's subscribe route and the TUI's queue both use this credential to subscribe through the server. The debug captures never write the value: `daemon.capture_web_downloads` elides every cookie value to `***` (see that row below). |
| `session.read_firefox_cookies` | bool | false | Send the local Firefox profile's **whole** `steamcommunity.com` cookie set on Workshop scrapes, in preference to the configured values. The store holds `steamLoginSecure` and, for scrapes, `sessionid` — `sessionid` is not HttpOnly — and reading them together for a scrape means the credential and the CSRF token cannot come from different sessions. (The profile read can never carry the *current* `sessionid` — it is a session cookie Firefox keeps in memory and never persists — so the subscribe path takes the page's own `g_sessionID` instead, with this read only a fallback.) This is what a real navigation sends: the browser's ten cookies for the domain, not two hand-picked names, and the scrape request is deliberately shaped to match a browser. Profiles are discovered, never named. The read is remembered for the process, but not past its own credential's expiry: while the remembered `steamLoginSecure` is live — or states no readable expiry — the set is served from the cache, and once its token says it has expired the profile is read again, so a daemon running for weeks picks up the cookie Steam reissues daily. An empty read is never cached. Off by default; when off, only the configured `sessionid`/`login_secure` pair is sent. Presence of a configured value is **not** evidence of a working session: a stale `login_secure` shadowed a good browser cookie and made every scrape anonymous, so the configured value is only a fallback and the response itself decides whether authentication is still good. |

---

## Security Boundaries

### Credential Protection

- API keys (`STEAM_API_KEY`, `OPENAI_API_KEY`) are loaded from environment variables or the config file. The config file should not contain these keys.
- `save_config` strips any config value that matches an environment variable before writing — this prevents accidentally persisting env-derived secrets.
- The `session.csrf_token` and `session.login_secure` are stored in the config file. These are Steam session cookies. `sessionid` is a CSRF token; `steamLoginSecure` is what authenticates the session, and it is what the subscribe call and the workshop scrape both send. Treat both as secrets. `steamLoginSecure` expires — *measured live* on 2026-09-17, the token carries `exp - iat` of 24.1 hours, so Steam reissues it about daily and an untouched profile goes stale within a day — and `/api/session/recheck` re-reads it from the browser's cookie store after the operator signs in and persists it, so the daemon picks it up without a restart. The value states its own expiry and `src/session_cookie.py` reads it, so code can refuse a dead cookie without asking Steam; when one is recorded, the web UI shows a warning with a link to sign in again ([web-ui.md](web-ui.md#the-session-warning)). When `session.read_firefox_cookies` is on, the Workshop scrape and the server-side subscribe call both send the profile's whole `steamcommunity.com` cookie set instead, which includes these two plus the browser's own non-credential state (`timezoneOffset`, `steamCountry`, `browserid`, and so on). That is deliberate — it is what a real navigation sends, and the request is shaped to match one — and none of the extra cookies is a credential.

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
  longest first: the current cookie set, `session.csrf_token` and `session.login_secure`
  in both accepted forms, `api.key` **and** `STEAM_API_KEY`, `openai.api_key`
  **and** `OPENAI_API_KEY` (`load_config` strips an env-derived key from the
  config before saving, so the config alone is not enough), plus anything
  registered at runtime through `crash.register_secret` — a refreshed
  `steamLoginSecure`;
* values are truncated and the whole file is capped at 256 KB, with the caps
  stated in the header.

This is **not** a complete barrier. It is key-name based and known-value based,
so a credential the process never registered, the config does not hold, and no
key name describes would still be written. The dump therefore goes only to the
owner's own outbox — it is collected by their sync and is never published — and
[docs/failure-capture.md](failure-capture.md) records the same residual risk.
With no outbox configured the dump falls back beside the configured log file,
else into the **application folder** (the program's own directory, not the
process working directory — a scheduled-task daemon's cwd is not where the
operator looks), and that path is printed to the console. At the next start the
outbox is known: every dump the fallback caught is moved into
`<outbox_dir>/crashes/` and registered in the manifest, so nothing stays
stranded outside it.

### Session Cookie Handling

- The `sessionid` cookie (not HttpOnly) is read from the configured value or the Firefox profile, and the subscribe route prefers the `g_sessionID` the item page itself carries.
- The `steamLoginSecure` cookie (HttpOnly) cannot be read by page JavaScript. The server-side subscribe endpoint reads it through `web_scraper._build_workshop_cookies` — the browser profile when `session.read_firefox_cookies` is on, otherwise the configured value — and refuses before making a request when neither source has one.
- For a Workshop scrape, `session.read_firefox_cookies` sends **every** `steamcommunity.com` cookie in the profile, not only the two above. A real Firefox navigation sends all of them, and the scrape request is deliberately shaped to match a real navigation; the additional names are benign browser state, not credentials. With the setting off, only the configured `sessionid`/`login_secure` pair is sent.
- The profile read is cached for the life of the process, but the cache does not outlive the credential it holds. `firefox_cookies.steam_community_cookies` serves the remembered set while the `steamLoginSecure` token is live or states no readable expiry — unknown is not treated as expired, so a value that cannot be decoded is still sent — and re-reads the whole profile once the token says it has expired. That re-read is a local file copy, not a request, and it is what lets a daemon that has been up for days stop sending a credential Steam has already killed; an empty read was never cached and still is not.
- No Steam credentials are ever sent to the browser. The `/api/subscribe` endpoint runs entirely server-side.

### Web Server

- The embedded Flask/Waitress server binds to all interfaces (`0.0.0.0`) by default. In production behind NAT or a firewall, this is accessible only within the local network. The randomly-assigned port provides minimal obscurity but not security.
- No authentication is implemented on the web server. All API endpoints are open to anyone with network access.
