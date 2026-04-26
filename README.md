# Steam Workshop Scraper & TUI

A robust, background Python daemon and Terminal User Interface (TUI) for scraping, storing, and querying Steam Workshop metadata.

## Features

*   **Persistent Background Daemon**: Continuously polls the Steam Web API and scrapes Steam Workshop HTML to build a comprehensive local database of mods, maps, and workshop items.
*   **Automatic Translation**: Can be configured with an OpenAI API key to automatically translate non-English titles and descriptions, which are then viewable in the TUI.
*   **Write-Ahead Logging (WAL)**: The SQLite database is configured with WAL mode, allowing the background daemon to write data at the exact same time you are querying it in the TUI without database locking errors.
*   **Advanced TUI**: A modern terminal interface powered by `Textual` with many advanced features:
    *   **State Persistence**: Remembers your filters, sorting, and last selected item between sessions.
    *   **Infinite Scrolling**: The item list automatically loads more results as you scroll down.
    *   **Command Palette**: Press `Ctrl+B` to access a command palette for quick actions.
    *   **Complex Search Builder**: Build complex queries with AND/OR logic, exact phrases (`"mickey mouse"`), negative terms (`-script`), and numeric inequalities (`>1000`).
    *   **BBCode Rendering**: Automatically converts Steam's BBCode formatting (like `[b]` and `[h1]`) into readable Markdown in the details pane.
    *   **Author Filtering**: Instantly pivot from an item to a list of all workshop items by that same creator.
*   **Unicode Safe**: Built to handle the massive variety of languages, emojis, and symbols found on the Steam Workshop without crashing or corrupting data.

## Installation

1.  Ensure you have Python 3.12+ installed.
2.  Clone the repository and navigate to the project directory:
    ```bash
    cd source/steam-workshop-scraper
    ```
3.  Create and activate a virtual environment:
    ```bash
    python3 -m venv .venv
    # For Bash/Zsh:
    source .venv/bin/activate
    # For Fish:
    source .venv/bin/activate.fish
    ```
4.  Install the project and its dependencies:
    ```bash
    pip install -e .
    ```
    *(Note: This creates the `workshop-daemon` and `workshop-tui` executables in `.venv/bin/`.)*

## Configuration

Before running the application, you need to configure it. Copy the provided example configuration file:

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml` to set your desired `batch_size`, `request_delay_seconds` (to avoid Steam rate limits), and the SQLite database path.

**Obtaining a Steam API Key:** You can obtain a free Steam Web API key directly from Valve by visiting the [Official Steam Community Developer Page](https://steamcommunity.com/dev/apikey). You will need to log in with your Steam account and provide a domain name (you can use `localhost` for personal/local development).

**API Key Security:** While you can place your Steam Web API Key directly in `config.yaml`, it is highly recommended to use the `STEAM_API_KEY` environment variable instead, which will override the YAML file:

```bash
export STEAM_API_KEY="YOUR_API_KEY_HERE"
```
*(Note: Public workshop items often do not require an API key, but having one is recommended for stability).*

**Translation (Optional):** To enable automatic translation of non-English text, you must provide an OpenAI API key. It is highly recommended to provide this as an environment variable:
```bash
export OPENAI_API_KEY="YOUR_OPENAI_API_KEY_HERE"
```

You can also configure the OpenAI endpoint and model in `config.yaml` if you wish to use a custom or self-hosted model.

## Usage

Ensure your virtual environment is activated so the commands in `.venv/bin/` are in your path. The project provides two tools:

### 1. The Daemon

Start the background worker. This process will read from the database to find items that need to be scraped, fetch their data from Steam, and save the results.

```bash
workshop-daemon
```
*Note: To run this silently in the background long-term, consider using `tmux`, `screen`, or a systemd service.*

### 2. The Terminal UI (TUI)

Open a new terminal window (ensure your virtual environment is activated) and launch the search interface:

```bash
workshop-tui
```

**Search Tips in the TUI:**
*   **Text Fields**: You can type multiple words to require all of them (AND logic).
*   **Negation**: Prefix a word with a minus sign to exclude it (e.g., `-broken`).
*   **Phrases**: Wrap words in quotes to search for exact phrases (e.g., `"cool mod"`).
*   **Numeric Fields**: Type `>= 1000` in the Subscriptions field to only see highly popular items.

## Development & Testing

This project was built using strict Test-Driven Development (TDD). The test suite includes unit tests, database concurrency tests, UI interaction tests, Unicode fuzzing, and live-internet contract verification.

To run the test suite and view coverage:

```bash
# Ensure dev dependencies are installed
pip install pytest pytest-cov responses hypothesis pytest-asyncio

# Run the helper script
python3 run_tests.py
```
