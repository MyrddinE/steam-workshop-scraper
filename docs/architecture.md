# Architecture

## Overview

The Steam Workshop Scraper is an automated system that aggregates, stores, and queries metadata
from the Steam Workshop. It pairs a background daemon for continuous data acquisition with a
Terminal User Interface (TUI) for exploring the collected dataset.

## System Architecture

The system follows a decoupled, specialized architecture:

1. **The Daemon (`src/daemon.py`)**: An orchestrator that manages the data acquisition pipeline.
2. **The TUI (`src/tui.py`)**: A consumer-facing interface for querying the database.
3. **The Database (`src/database.py`)**: A shared SQLite persistence layer.
4. **The Translator (`src/translator.py`)**: A background service for language translation.
5. **Data Sources**: A hybrid approach using the official Steam Web API for metadata and discovery,
   plus HTML scraping for the fields the API does not expose.

## Components

### The Daemon (orchestrator)

The daemon operates as a continuous loop, managing data acquisition, enrichment, and discovery.

#### Discovery and seeding

The daemon does not merely wait for new items; it actively expands the database:

* **Cursor-based discovery (`seed_database`)**: Walks `IPublishedFileService/QueryFiles` sorted by
  publication date, resuming from the cursor stored in `app_discovery.last_cursor`. Each page
  inserts newly seen `publishedfileid` values as bare rows; metadata is filled in later.
* **Page-based discovery (`_run_page_discovery`)**: Once the cursor is exhausted — or at least 500
  items have been scraped — the daemon periodically walks the same endpoint sorted by last-updated
  time. It compares each item's Steam `steam_updated_at` against the stored value and queues
  genuinely new or changed items for fetching at `api_priority = 5`. This catches content changes
  that publication-order scanning would never revisit. It runs at most once per 24 hours.
* **Proactive user expansion**: The daemon identifies creators whose profile information is missing
  or stale and refreshes their persona details via the Steam API (`GetPlayerSummaries/v2`).
* **Dynamic scrape delay**: To avoid rate-limiting and adapt to network conditions, the daemon
  adjusts the delay between requests. It is counted in **requests**, not items: one batched
  `GetPublishedFileDetails` call is one data point, so a batch whose items are individually
  "not found" cannot masquerade as a run of failures. The rule is TCP congestion control — each
  refused request (a transport error, a timeout, an HTTP error such as 429/5xx, or an unparseable
  body) doubles the delay, and healthy operation halves it for every ten minutes it has been
  running — so the delay converges to just under whatever rate Steam will sustain and keeps probing
  that moving limit. The recovery is measured in **time** rather than in successful requests, so
  every queue recovers over the same wall-clock window; `src/pacing.py` holds the rule for all
  three. Individual item results drive item state and never touch the delay.

#### The scrape pipeline

For every identified `workshop_id`, the daemon executes a multi-stage enrichment process:

1. **Primary fetch (API)**: Retrieves core metadata (title, creator, tags, counts, preview URL)
   using `GetPublishedFileDetails/v1`, one request per batch of ids rather than per item.
2. **Deep enrichment (web scraper)**: The API does not expose the full body text, so a worker
   thread visits the item's HTML page to extract the extended description and tags using CSS
   selectors. Tags from the API and the scraper are merged.
3. **Identity enrichment (user API)**: Updates the `creators` table with the latest creator
   information.
4. **Translation flagging**: Non-ASCII text fields are added to the translation queue at a
   priority derived from the item's fetch priority. The title and short description are flagged by
   the daemon, the extended description by the web scraper, and creator names by the user refresh.

#### Reliability

* **Graceful shutdown**: Signal handling (`SIGINT`, `SIGTERM`) lets batches finish and threads close
  cleanly.
* **Partial data handling**: A failed API request is classified once. A permanent failure (404) is
  recorded, the item is marked dead (`-1`) and it leaves the queue; anything else — `500`, a
  transport exception, or a status no branch handles — is recorded and the item is re-queued one
  priority level lower, floored at `1`, so it is retried behind current work rather than dropped. A
  failed web scrape leaves the item's `web_scrape_priority` priority in place so it is retried, while
  the metadata already fetched stays usable; a selector miss leaves priority alone when the page was
  not the item's, and clears it only when the item page genuinely carries no description.
* **Error handling**: A failure is either recovered from or reported. A handler that recovers logs
  the operation and the exception; silence is reserved for failures already represented by a return
  value or a status column — a best-effort cleanup, an optional probe — and each such handler states
  why silence is safe. Broad `except Exception` is uncommon and names what it protects.
* **Failure capture**: When the scraper meets input it cannot handle — a selector that no longer
  matches, an API body that is not JSON, an API status with no branch — the response is written to
  the pull-outbox as a bounded, structured artefact so a regression test can be built from it, and
  registered in the same manifest the database snapshots use. Off unless an outbox is configured.
  See [failure-capture.md](failure-capture.md).

### The Terminal User Interface (TUI)

The TUI is an interface for exploring the scraped data, built with the `Textual` framework.

* **Advanced search builder**: Constructs complex queries with multiple AND/OR conditions across
  any field, including text matching, numeric inequalities, and ID lookups.
* **State persistence**: Saves filters, sorting preferences, scroll position, and the last-viewed
  item to a local `.tui_state.yaml` file so a session can be resumed later.
* **Infinite scrolling**: As the user scrolls toward the bottom, the TUI fetches and appends the
  next page of results.
* **Dynamic details pane**:
  * **BBCode-to-Markdown**: Converts Steam's BBCode into Markdown for display.
  * **Translation toggle**: Toggles between the original and translated text for translated items.
  * **Author jump**: Re-runs the search for all items by the currently viewed creator, as a
    single-creator mode that replaces the filters and can be returned from.
* **Command palette**: A searchable menu for actions such as clearing the database or managing
  filters.

### Front-end parity (TUI and Web UI)

Both front ends read the same database and the same shared tables — `src/pending.py` for a stage
marker and `src/subscription.py` for the subscription marker — so a change on one side is reviewed
against the other in the same change. **Layout may differ**: a terminal and a browser have different
constraints, so the same control can sit in a different place, in a different order, and be drawn
differently. **Function may not**: the same data is surfaced and the same actions are available on
both sides. When one front end gains a metric, a marker state, a control or an estimate, the other
gains it too, or the difference is recorded with the reason it cannot be shared. A gap noticed while
working on one side is fixed at once when the change is small and obvious, and raised as a question
when the two sides genuinely need different behaviour — [tui.md](tui.md), [web-ui.md](web-ui.md).

### The Data Layer (SQLite)

The database is designed for high-concurrency and complex querying. See
[data-model.md](data-model.md) for the full column reference.

* **Concurrency model**: Uses SQLite WAL (Write-Ahead Logging) mode, so the daemon can write while
  the TUI performs heavy read operations without locking. The mode is a persistent property of the
  database file and is established **once**, by `initialize_database`, which every entry point calls
  before it reads or writes. `get_connection` deliberately runs no `PRAGMA journal_mode`: a
  journal-mode statement is not covered by the connection's busy timeout, so a reader that ran it on
  every connection could be refused while another process held a lock — which is how a two-second
  TUI poll took the session down (issue 43). The unattended TUI polls additionally skip a tick they
  could not read rather than ending the session; see [tui.md](tui.md).
* **Schema**:
  * **`workshop_items`**: Item metadata. Key columns include `workshop_id` (PK), `fetch_status`
    (HTTP-like status code), `title`, `creator_steamid`, `extended_description`, the three timestamp
    clocks (`steam_created_at`, `steam_updated_at`, `first_seen_at`), and translation fields
    (`translation_priority`, `translate_version`, `title_en`, and so on). Lifetime stats live in
    `lifetime_subscriptions` and `lifetime_favorited`.
  * **`tags` / `workshop_tags`**: A normalized tag store and an item-to-tag junction table.
    `workshop_items` has no tags column.
  * **`creators`**: Creator information, keyed by `steamid`, with translated name fields.
  * **`app_discovery`**: Per-AppID discovery state (`last_cursor`) and the
    enrichment filters that gate web scraping.
* **Schema evolution**: A brand-new database is created directly at the current schema version; an
  existing one is carried forward by built-in migration logic that adds and renames columns without
  data loss. A database whose recorded version is **newer** than the running build is refused before
  anything is written — the older build cannot know what the newer schema means, so it stops with
  `SchemaVersionError` and a message naming the file and both versions rather than reading and
  writing a schema it does not understand. The remedy is a build at least as new as the file: the
  database is not corrupt, and rolling back to an older build is not a way out once the schema
  renames have run. See [schema-migrations.md](schema-migrations.md).

### The Translator

The translation system runs as a separate background thread, in parallel with the daemon and TUI.

* **Workflow**: The thread continuously polls `translation_queue` for fields to translate. It
  fetches the highest-priority candidates, packs them into a request by size, sends the non-ASCII
  text fields to an external API, and writes the English results back to the corresponding `_en`
  columns. A reply covering only part of its request is a partial success: the fields it answered
  are written and the rest stay queued.
* **OpenAI integration**: Uses an OpenAI-compatible API by default (`gpt-4o-mini`). The request is
  written as boundary blocks in the same shape as the reply it asks for — each opening with a phrase
  of four words drawn fresh for that request — so the model copies a structure rather than building
  one and never has to escape the translated text. (It previously asked for a JSON object "to make
  parsing reliable"; the escaping that required turned out to be the least reliable part of it — see
  [data-pipeline.md](data-pipeline.md#translation-phase).)
* **Configuration**: Configured via the `openai` section in `config.yaml`, with the API key
  supplied through the `OPENAI_API_KEY` environment variable.

### External Interfaces

* **Steam Web API**: The structured source for metadata, discovery, and user information.
* **HTML scraper**: An enrichment layer that parses Steam Workshop pages for the extended
  description and tags.
* **OpenAI API**: An optional external service used for language translation.

## Technical Highlights

* **Efficiency**: "Check-before-scrape" logic minimizes redundant API calls.
* **Robustness**: Error handling at every step (API failures, scraping errors, schema mismatches)
  keeps partial records usable when only some data sources are available.
* **Complexity management**: Unicode-safe processing and a prioritized translation pipeline handle
  the multi-language nature of the Steam Workshop.
