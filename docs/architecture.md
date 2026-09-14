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
  publication date, resuming from the cursor stored in `app_tracking.last_cursor`. Each page
  inserts newly seen `publishedfileid` values as bare rows; metadata is filled in later.
* **Page-based discovery (`_run_page_discovery`)**: Once the cursor is exhausted — or at least 500
  items have been scraped — the daemon periodically walks the same endpoint sorted by last-updated
  time. It compares each item's Steam `steam_updated_at` against the stored value and queues
  genuinely new or changed items for fetching at `api_priority = 5`. This catches content changes
  that publication-order scanning would never revisit. It runs at most once per 24 hours.
* **Proactive user expansion**: The daemon identifies creators whose profile information is missing
  or stale and refreshes their persona details via the Steam API (`GetPlayerSummaries/v2`).
* **Dynamic scrape delay**: To avoid rate-limiting and adapt to network conditions, the daemon
  adjusts the delay between requests. It decreases the delay after long periods of success and
  increases it after a series of consecutive failures.

#### The scrape pipeline

For every identified `workshop_id`, the daemon executes a multi-stage enrichment process:

1. **Primary fetch (API)**: Retrieves core metadata (title, creator, tags, counts, preview URL)
   using `GetPublishedFileDetails/v1`.
2. **Deep enrichment (web scraper)**: The API does not expose the full body text, so a worker
   thread visits the item's HTML page to extract the extended description and tags using CSS
   selectors. Tags from the API and the scraper are merged.
3. **Identity enrichment (user API)**: Updates the `users` table with the latest creator
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
  failed web scrape leaves the item's `needs_web_scrape` priority in place so it is retried, while
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
  * **Author jump**: Re-runs the search for all items by the currently viewed creator.
* **Command palette**: A searchable menu for actions such as clearing the database or managing
  filters.

### The Data Layer (SQLite)

The database is designed for high-concurrency and complex querying. See
[data-model.md](data-model.md) for the full column reference.

* **Concurrency model**: Uses SQLite WAL (Write-Ahead Logging) mode, so the daemon can write while
  the TUI performs heavy read operations without locking.
* **Schema**:
  * **`workshop_items`**: Item metadata. Key columns include `workshop_id` (PK), `status`
    (HTTP-like status code), `title`, `creator`, `extended_description`, the three timestamp
    clocks (`steam_created_at`, `steam_updated_at`, `first_seen_at`), and translation fields
    (`translation_priority`, `translate_version`, `title_en`, and so on). Lifetime stats live in
    `lifetime_subscriptions` and `lifetime_favorited`.
  * **`tags` / `workshop_tags`**: A normalized tag store and an item-to-tag junction table.
    `workshop_items` has no tags column.
  * **`users`**: Creator information, keyed by `steamid`, with translated name fields.
  * **`app_tracking`**: Per-AppID discovery state (`last_cursor`, `last_page_scanned`) and the
    enrichment filters that gate web scraping.
* **Schema evolution**: Built-in migration logic adds and renames columns on existing databases
  without data loss. See [schema-migrations.md](schema-migrations.md).

### The Translator

The translation system runs as a separate background thread, in parallel with the daemon and TUI.

* **Workflow**: The thread continuously polls `translation_queue` for fields to translate. It
  fetches the highest-priority batch, sends the non-ASCII text fields to an external API, and
  writes the English results back to the corresponding `_en` columns.
* **OpenAI integration**: Uses the OpenAI API by default (`gpt-4o-mini`). The prompt requests a
  JSON object as output to make parsing reliable.
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
