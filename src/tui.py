import json
import logging
import math
import time
from functools import partial
from textual.app import App, ComposeResult
from textual import on, events
from textual.command import Provider, Hit, DiscoveryHit
from textual.system_commands import SystemCommandsProvider
from typing import Iterable, NamedTuple
from textual.screen import Screen, ModalScreen
from textual.widgets import Header, Footer, Input, ListView, ListItem, Static, Label, Select, Button, Markdown, DataTable, RichLog
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.worker import Worker, WorkerState
from src.database import search_items, get_all_creator_ids, SchemaVersionError, get_item_details, save_enrichment_filters, delete_never_fetched_items, toggle_subscription_queue, get_subscription_queue_items, compute_wilson_cutoffs, raise_web_scrape_priority_for_list, raise_web_scrape_priority_for_detail, raise_translation_priority_for_list, raise_translation_priority_for_detail, raise_image_priority_for_list, raise_image_priority_for_detail, get_connection, SEARCH_FILTER_SCHEMA, ALL_FILTER_FIELDS, raise_api_priority_for_list, raise_api_priority_for_detail, get_subscription_states, get_items_by_ids, SUBSCRIBED_FIELD, SUBSCRIBED_VALUES, normalise_subscribed_value, live_fetch_status_predicate, toggle_ignored_item, IGNORED_FETCH_STATUS, creator_is_ignored, creator_ignore_label, toggle_creator_ignored
from src.analysis import view_window_analysis
from src import metrics
from src import db_poll
from src import activity
from src import images
from src import item_updates
from src import pending
from src import subscription
from src import subscribe_engine
from src import crash
from src import log_rotation
from src import workshop_folders
from src.web_worker import configured_web_delay
from src.config import ConfigError, load_config, save_config
from src.daemon_control import (
    DaemonController,
    DaemonStillRunningError,
    initialize_database_with_daemon_stopped,
)
import os
# Module level, not just inside `main`: `ScraperApp.__init__` prints the
# ConfigError and SchemaVersionError refusals to stderr before `main` ever runs,
# and the old in-`main` import left those branches with a NameError.
import sys
import yaml
import threading
import datetime

# Field types and enum values from the central schema. The builder's value
# control is an Input for every free-text field and a Select for an enum one
# (`Subscribed` is the only one), so the decision and the choices both come from
# SEARCH_FILTER_SCHEMA rather than a second copy of the field list here.
_FIELD_TYPES = {f["field"]: f["type"] for f in SEARCH_FILTER_SCHEMA}
_FIELD_VALUES = {f["field"]: list(f.get("values", [])) for f in SEARCH_FILTER_SCHEMA}

SUBSCRIBED_OVERLAY_TOOLTIP = (
    "Disabled because the filter builder already has a Subscribed row. Two "
    "constraints on the same field are redundant or contradictory."
)


def _enum_value_options(field: str, op) -> list[tuple[str, str]]:
    """The choices an enum value control offers, with ``any`` dropped for is_not.

    ``is_not any`` would match nothing, so it is not offered as a choice. A value
    that arrives from a saved filter is preserved separately by the row's
    ``_sync_value_control``.
    """
    values = _FIELD_VALUES.get(field, [])
    if op == "is_not":
        values = [v for v in values if v != "any"]
    return [(v, v) for v in values]


def escape_markup(value) -> str:
    """Escape Steam-derived text before interpolating it into Rich markup.

    Steam titles, tag names and persona names routinely contain square
    brackets -- *measured live*, 129,533 titles in this library hold a bracket
    pair, and 2 of the 9 items queued for subscription did -- and a bracket
    pair is markup to `Text.from_markup`. One that names a real style is
    silently swallowed; one that does not is a ``MissingStyle`` error that takes
    the whole screen down, which is exactly how ``[najar]偶像大师 ...`` crashed
    the subscription queue.

    Every value that comes from Steam passes through here before it joins a
    markup string. The markup this module writes itself (``[b]``, colours, the
    spinner, the ``[link=...]`` the queue builds from its own URL) is not
    escaped. Textual widgets and `DataTable` cells parse markup from ``str``
    too, so the helper is used for plain ``Label.update`` calls as well as
    f-strings.

    ``rich.markup.escape`` is not enough on its own: it only escapes ``[...]``
    that already look like a tag, so an unbalanced ``[`` is left standing and
    Textual's parser then swallows everything up to the next ``]`` -- including
    the project's own closing tag. Escaping every ``[`` is exact for Textual's
    parser, which treats ``\\[`` as a literal bracket. Returns a ``str`` so it
    drops straight into an f-string.
    """
    return str(value).replace("[", "\\[")

def format_ts(ts):
    """Converts a Unix timestamp to YYYY-MM-DD string."""
    if not ts: return "N/A"
    try:
        return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
    except Exception:
        logging.debug("format_ts failed for value %r", ts)
        return "N/A"

def format_size(size_bytes):
    """Converts bytes to human-readable KB/MB/GB with Rich markup and
    3-significant-digit precision.  Color thresholds: >=10GB red, >=1GB
    yellow, >=100MB white, below that gray."""
    if not size_bytes: return "N/A"
    try:
        b = float(size_bytes)
        mb = b / (1024 * 1024)
        if mb < 0.1:
            return f"[gray]{b/1024:.1f} KB[/gray]"
        if mb < 10:
            return f"[gray]{mb:.2f} MB[/gray]"
        if mb < 100:
            return f"[gray]{mb:.1f} MB[/gray]"
        if mb < 1000:
            return f"[white]{mb:.0f} MB[/white]"
        gb = mb / 1024
        if gb < 10:
            return f"[yellow]{gb:.2f} GB[/yellow]"
        if gb < 100:
            return f"[red]{gb:.1f} GB[/red]"
        return f"[red]{gb:.0f} GB[/red]"
    except Exception:
        logging.debug("format_size failed for value %r", size_bytes)
        return "N/A"

def format_count(n):
    """Humanizes a number to 3 significant digits with K/M suffix and color markup.

    A zero is a measured zero and reads "0"; only a value that is missing or
    cannot be coerced to a number is unknown and reads "N/A".
    """
    if n is None or n == "":
        return "[gray]N/A[/gray]"
    try:
        n = int(n)
    except (ValueError, TypeError):
        return "[gray]N/A[/gray]"
    if n < 1000:
        return f"[gray]{n}[/gray]"
    if n < 1_000_000:
        if n < 10_000:
            return f"[white]{n/1000:.2f}K[/white]"
        elif n < 100_000:
            return f"[white]{n/1000:.1f}K[/white]"
        else:
            return f"[white]{n/1000:.0f}K[/white]"
    # >= 1M
    millions = n / 1_000_000
    if n < 10_000_000:
        return f"[yellow]{millions:.2f}M[/yellow]"
    elif n < 100_000_000:
        return f"[yellow]{millions:.1f}M[/yellow]"
    else:
        return f"[yellow]{millions:.0f}M[/yellow]"

def parse_tags(tags) -> list[str]:
    """Parses a comma-separated tag string (junction-table format) or legacy
    JSON into a list of tag-name strings."""
    if not tags:
        return []
    # Junction-table format: comma-separated via GROUP_CONCAT(t.tag_name, ', ')
    if isinstance(tags, str) and not tags.startswith('['):
        return [tag.strip() for tag in tags.split(',') if tag.strip()]
    # Legacy JSON format
    try:
        parsed = json.loads(tags) if isinstance(tags, str) else tags
        return [str(tag.get("tag") if isinstance(tag, dict) else tag) for tag in (parsed if isinstance(parsed, list) else [])]
    except Exception:
        logging.debug("parse_tags: failed to parse %r", tags)
        return []

class StatsScreen(Screen):
    """Database statistics, one independent chunk per metric.

    Nothing here is classified or grouped. Each metric owns exactly one section
    and one widget, and a chunk is drawn the moment its query returns without
    touching any other, so the screen fills in whatever order the queries finish.

    The only global decision is the order the metrics are *requested* in. On the
    first pass nothing has been measured, so it is ``metrics.all_names()`` -- the
    seed order. Afterwards each metric is placed by what it actually cost last
    time, so the order follows the data: if a query gets cheap or expensive, the
    display reorders itself with no code change. Cheapest-first also means a slow
    query is never started ahead of a fast one that is already due.

    Refresh is per metric as well. A metric is re-run once its own interval --
    ``REFRESH_FACTOR`` times its own measured duration, floored at
    ``MIN_REFRESH_SECONDS`` -- has elapsed. The pre-rework screen applied one such
    rule to the whole payload, so the slowest query stretched every other refresh
    out to minutes.
    """

    #: How many measured durations a metric waits before it is asked for again.
    REFRESH_FACTOR = 50.0
    #: ...but never faster than this, so a metric that returns in under a
    #: millisecond is not re-run several times a second.
    MIN_REFRESH_SECONDS = 2.0
    #: How often the UI thread checks which metrics are due. This is only the
    #: granularity of the schedule; the throttle itself is each metric's own
    #: interval.
    SCHEDULER_TICK_SECONDS = 1.0

    #: The one metric that gets its own column rather than a section in the
    #: scrolling list: it is a long table, not a handful of numbers.
    TAG_METRIC = "tag_counts"

    #: Human labels for the section headings, kept in step with the web panel's.
    METRIC_LABELS = {
        "high_water": "Last successful API fetch",
        "item_counts": "Totals",
        "app_discovery": "App discovery",
        "status_counts": "Status counts",
        "dead_items_by_queue": "Stuck work",
        "dead_queued": "Dead but queued",
        "queued_nowhere": "Queued nowhere",
        "fetch_recency": "Fetch recency",
        "coverage": "Coverage",
        "translation_status": "Translation status",
        "tag_counts": "Tags",
        "priority_breakdowns": "Queue priorities",
        "web_throughput": "Web scrape throughput",
        "image_throughput": "Image download throughput",
        "translation_throughput": "Translation throughput",
        "queue_eta": "Time to drain",
    }

    #: Text metrics own a Static widget; the two table metrics are special-cased
    #: in `_compose_metric_section` and `_render_metric`.
    METRIC_CONTENT_IDS = {
        "high_water": "high-water-content",
        "item_counts": "item-counts-content",
        "status_counts": "status-content",
        "dead_items_by_queue": "dead-items-by-queue-content",
        "dead_queued": "dead-queued-content",
        "queued_nowhere": "queued-nowhere-content",
        "fetch_recency": "recency-content",
        "coverage": "coverage-content",
        "translation_status": "translation-stats-content",
        "priority_breakdowns": "priority-stats-content",
        "web_throughput": "web-throughput-content",
        "image_throughput": "image-throughput-content",
        "translation_throughput": "translation-throughput-content",
        "queue_eta": "queue-eta-content",
    }

    def __init__(self, db_path: str, target_appids: list | None = None,
                 staleness_days: int | None = None):
        super().__init__()
        self.db_path = db_path
        #: The configured target AppIDs, when the caller has them. The coverage
        #: metric restricts its second figure to these apps' enrichment filters;
        #: when this is None the metric falls back to every `app_discovery` row.
        self.target_appids = target_appids
        #: The item re-fetch window (`daemon.item_staleness_days`), read through
        #: `metrics.item_staleness_days` by the caller so the recency figure and
        #: the daemon's sweep use one number. A caller that has no config (tests
        #: constructing the screen directly) gets the daemon's own default.
        self.staleness_days = (
            metrics.DEFAULT_STALENESS_DAYS if staleness_days is None
            else int(staleness_days)
        )
        #: Last measured duration per metric, and when it finished, both kept for
        #: the session so the request order and the intervals adapt to the data.
        self._measured_ms: dict[str, float] = {}
        self._ran_at: dict[str, float] = {}
        self._intervals: dict[str, float] = {}
        #: Metrics in the running pass, so one still computing is never started
        #: again.
        self._inflight: set[str] = set()
        self._pass_running = False

    def compose(self) -> ComposeResult:
        yield Header()
        # Two columns, as the screen had before the per-metric rework: the
        # metrics flow down the left, and the tag table keeps a block of its own
        # on the right, filling the height. Tags are the one metric that is a
        # long list rather than a few numbers, so folding it into the same
        # column pushed every section below it off the screen.
        with Horizontal(id="stats-main"):
            with VerticalScroll(id="stats-scroll"):
                for name in metrics.all_names():
                    if name == self.TAG_METRIC:
                        continue
                    yield from self._compose_metric_section(name)
            with Vertical(id="stats-right-col"):
                yield Label(
                    f"[b]{self.METRIC_LABELS[self.TAG_METRIC]}[/b]",
                    id=f"stats-label-{self.TAG_METRIC}",
                    classes="stats-header",
                )
                with VerticalScroll(id="tag-stats-scroll"):
                    yield DataTable(id="tag-stats-table")
        yield Footer()
        yield Button("Close", id="btn-close-stats")

    def _compose_metric_section(self, name: str):
        """One metric's section: a heading, and the one widget only it writes."""
        with Vertical(classes="stats-section", id=f"chunk-{name}"):
            yield Label(
                f"[b]{self.METRIC_LABELS.get(name, name)}[/b]",
                id=f"stats-label-{name}",
                classes="stats-header",
            )
            if name == "app_discovery":
                yield DataTable(id="app-stats-table")
            else:
                yield Static(id=self.METRIC_CONTENT_IDS[name])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-close-stats":
            self.app.pop_screen()

    def on_mount(self) -> None:
        # Placeholders name every section while it is still computing; the first
        # pass then starts without waiting for the scheduler's first tick.
        for widget_id in self.METRIC_CONTENT_IDS.values():
            self.query_one(f"#{widget_id}", Static).update("[dim]Computing…[/dim]")
        self._start_due_metrics()
        self.set_interval(self.SCHEDULER_TICK_SECONDS, self._on_scheduler_tick)

    def on_unmount(self) -> None:
        # A pass can be mid-query when the screen closes. Cancel the group so a
        # late result cannot touch widgets that are already gone.
        self.workers.cancel_group(self, "stats")

    # ------------------------------------------------------------------
    # scheduling
    # ------------------------------------------------------------------

    def _interval_for(self, duration_ms: float) -> float:
        """How long a metric that cost ``duration_ms`` waits before re-running."""
        return max(self.MIN_REFRESH_SECONDS, self.REFRESH_FACTOR * duration_ms / 1000.0)

    def _request_order(self) -> list[str]:
        """Metric names, cheapest first.

        Seed order until something has been measured, then measured durations, so
        the request order follows the data. A metric with no measurement of its
        own yet keeps its seed estimate.
        """
        def estimate(name: str) -> float:
            return self._measured_ms.get(name, metrics.REGISTRY[name].seed_ms)

        return sorted(metrics.all_names(), key=lambda n: (estimate(n), n))

    def _is_due(self, name: str, now: float) -> bool:
        if name in self._inflight:
            return False
        last = self._ran_at.get(name)
        if last is None:
            return True
        return now - last >= self._intervals.get(name, self.MIN_REFRESH_SECONDS)

    def _due_metrics(self, now: float) -> list[str]:
        """The metrics whose own interval has elapsed, cheapest first."""
        return [name for name in self._request_order() if self._is_due(name, now)]

    def _start_due_metrics(self) -> None:
        """Start one thread worker over every due metric, if none is running."""
        if self._pass_running or not self.is_mounted:
            return
        due = self._due_metrics(time.monotonic())
        if not due:
            return
        self._pass_running = True
        self._inflight.update(due)
        self.run_worker(
            partial(self._stream_metrics, due),
            name="stats",
            group="stats",
            thread=True,
            exit_on_error=False,
        )

    def _on_scheduler_tick(self) -> None:
        """Timer callback: let the worker decide what is due."""
        self._start_due_metrics()

    def _stream_metrics(self, names: list[str]) -> None:
        """Worker body: compute each due metric and hand it to the UI as it lands.

        ``iter_metrics`` shares one connection across the pass, but it is a
        generator, so a chunk is applied the moment its own query returns rather
        than when the slowest one does.
        """
        from src.database import compact_tag_ids

        for name, entry in metrics.iter_metrics(
                self.db_path, names,
                {"target_appids": self.target_appids,
                 "staleness_days": self.staleness_days}):
            if name == "tag_counts":
                # The tag-frequency write stays off the UI thread, exactly as the
                # old slow-tier worker did, and runs once per tag metric arrival.
                tag_counts = entry.get("value") or {}
                if tag_counts:
                    compact_tag_ids(self.db_path, tag_counts)
            try:
                self.app.call_from_thread(self._apply_metric, name, entry)
            except RuntimeError:
                # The app stopped while this query was in flight: there is no UI
                # left to apply to, and no point computing the rest.
                logging.debug("[stats] dropped %s; the app is no longer running", name)
                return

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.group != "stats":
            return
        if event.state in (WorkerState.ERROR, WorkerState.CANCELLED):
            logging.warning("[stats] metric pass %s: %s", event.state, event.worker.error)
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            self._pass_running = False
            self._inflight.clear()

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------

    def _apply_metric(self, name: str, entry: dict) -> None:
        """UI-thread callback: record what one metric cost and draw its chunk."""
        if not self.is_mounted:
            return
        duration = entry.get("ms") or 0.0
        self._measured_ms[name] = duration
        self._ran_at[name] = time.monotonic()
        self._intervals[name] = self._interval_for(duration)
        self._inflight.discard(name)
        self._render_metric(name, entry.get("value"))
        self.query_one(f"#stats-label-{name}", Label).update(
            f"[b]{self.METRIC_LABELS.get(name, name)}[/b] [dim]{duration:.1f} ms[/dim]"
        )

    def _set_text(self, name: str, text: str) -> None:
        self.query_one(f"#{self.METRIC_CONTENT_IDS[name]}", Static).update(text)

    def _render_metric(self, name: str, value) -> None:
        """Draw one metric's value into the widget that metric owns."""
        # high_water answers None legitimately -- no successful fetch yet -- so it
        # is handled before the failed-metric guard rather than shown as an error.
        if name == "high_water":
            fetched = (
                datetime.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
                if value else "never"
            )
            self._set_text(name, fetched)
        elif value is None:
            if name in self.METRIC_CONTENT_IDS:
                self._set_text(name, "[dim]unavailable[/dim]")
            elif name in ("app_discovery", "tag_counts"):
                self.query_one(
                    "#app-stats-table" if name == "app_discovery" else "#tag-stats-table",
                    DataTable,
                ).clear(columns=True)
        elif name == "item_counts":
            self._set_text(
                name,
                f"[b]Live items:[/b] {value.get('alive', 0):,}   "
                f"[b]Dead:[/b] {value.get('dead', 0):,}   "
                f"[b]Ignored:[/b] {value.get('ignored', 0):,}   "
                f"[dim](total {value.get('total', 0):,})[/dim]",
            )
        elif name == "app_discovery":
            table = self.query_one("#app-stats-table", DataTable)
            table.clear(columns=True)
            table.add_columns("AppID", "Last Cursor")
            for app in value:
                cursor = str(app.get("last_cursor", "") or "")
                table.add_row(
                    str(app.get("appid")),
                    escape_markup(cursor[:30] + "..." if len(cursor) > 30 else cursor),
                )
        elif name == "status_counts":
            lines = [
                f"  Status {row.get('fetch_status')}: {row.get('count', 0):,}"
                for row in value
            ]
            self._set_text(
                name,
                "[b]Record count by status[/b]\n" + ("\n".join(lines) or "  (none)"),
            )
        elif name == "dead_items_by_queue":
            self._set_text(name, self._format_dead_items_by_queue(value))
        elif name == "dead_queued":
            self._set_text(name, self._format_handoff_metric(
                value,
                "No dead item is still in a work queue.",
                "dead item(s) are still in a work queue",
            ))
        elif name == "queued_nowhere":
            self._set_text(name, self._format_handoff_metric(
                value,
                "No item is stranded: every live item is queued or complete.",
                "item(s) are in no queue and not complete",
            ))
        elif name == "fetch_recency":
            # The window is the metric's own answer, not a constant here, so a
            # label can never name a different window than the query looked at.
            window = int(value.get("window_days", metrics.DEFAULT_STALENESS_DAYS))
            self._set_text(
                name,
                "[b]Record count by fetch recency[/b]\n"
                f"  Fresh (last {window}d): {value.get('fresh', 0):,}\n"
                f"  Stale (over {window}d): {value.get('stale', 0):,}\n"
                f"  Never attempted: {value.get('unknown', 0):,}\n"
                f"[dim]{metrics.FETCH_RECENCY_MEANING}[/dim]",
            )
        elif name == "coverage":
            self._set_text(name, self._format_coverage(value))
        elif name == "translation_status":
            self._set_text(
                name,
                "\n".join(f"  {status}: {count:,}" for status, count in value.items())
                or "[dim]No data.[/dim]",
            )
        elif name == "tag_counts":
            table = self.query_one("#tag-stats-table", DataTable)
            table.clear(columns=True)
            table.add_columns("Tag", "Count")
            for tag, count in sorted(value.items(), key=lambda kv: kv[1], reverse=True):
                table.add_row(escape_markup(tag), f"{count:,}")
        elif name == "priority_breakdowns":
            self._set_text(name, self._format_priority(value))
        elif name in ("web_throughput", "image_throughput", "translation_throughput"):
            self._set_text(name, self._format_throughput(value))
        elif name == "queue_eta":
            self._set_text(name, self._format_queue_eta(value))

    @staticmethod
    def _format_throughput(value: dict) -> str:
        """A queue's completion rate, or an honest "no history yet".

        A NULL stamp is not a zero: it means the stage finished before the
        column that records the time existed, so no rate can be computed from
        it. Showing 0/hour would read as an idle queue rather than an
        unmeasurable one -- the one thing the metric must not do is invent a
        number. Once any stamp exists a 0 is a real measurement and is shown.
        """
        if not value or value.get("last_success") is None:
            return ("[dim]No history yet — completion times are only recorded "
                    "from here on.[/dim]")
        last = datetime.datetime.fromtimestamp(
            value["last_success"]).strftime("%Y-%m-%d %H:%M")
        return (f"  Completed last hour: {value.get('last_hour', 0):,}\n"
                f"  Completed last day: {value.get('last_day', 0):,}\n"
                f"  Last success: {last}")

    #: Standard and subsidiary bar glyphs. A translation bar hangs off the bar
    #: above it, so it is drawn with lower-half/lower-eighth blocks where the
    #: standard bar uses a full cell and a shade: visibly thinner in height,
    #: with the same left edge and the same length semantics, so a shorter bar
    #: still means less coverage. There is no way to shrink a line of text's
    #: height, and shrinking the bar's *length* would be read as coverage, so
    #: the glyph carries the subordination instead.
    #:
    #: The middle glyph is the translation bars' **no-work** segment -- the
    #: fields on the track that need no translation at all. It sits between the
    #: full cell and the light shade so the three parts of the track stay
    #: distinguishable without colour: done, needs nothing, still to do.
    COVERAGE_BAR_WIDTH = 20
    COVERAGE_GLYPHS = {
        False: ("█", "▒", "░"),
        True: ("▄", "▂", "▁"),
    }

    @staticmethod
    def _coverage_bar_line(bar: dict, label_width: int) -> str:
        """One bar: a label, the percentage of its track, the bar, the counts.

        ``pct``, the gray share and the counts all come from the metric rather
        than being recomputed here, so the terminal and the browser print the
        same numbers and draw the same segments. A translation bar's track has
        three parts: the green fill for the share done, the gray no-work segment
        for the slots that need no translation, and the empty track for what is
        left to do. A bar whose reachable population is zero shows its empty
        track and the metric's own words instead of a percentage that would sit
        at 0.0% forever.
        """
        label = f"{bar.get('label') or bar.get('key', ''):<{label_width}}"
        width = StatsScreen.COVERAGE_BAR_WIDTH
        on, gray_glyph, off = StatsScreen.COVERAGE_GLYPHS[bool(bar.get("subsidiary"))]
        maximum = bar.get("maximum", 0) or 0
        if maximum <= 0:
            # The bar keeps its column even with no percentage, so every bar
            # shares one left edge and a length still means the same thing.
            track = f"[dim]{off * width}[/dim]"
            return f"{label} {' ' * 6}  {track}  {bar.get('empty') or 'Nothing to reach'}"
        pct = bar.get("pct") or 0.0
        filled = max(0, min(width, int(round(pct / 100 * width))))
        gray_pct = bar.get("gray_pct")
        if gray_pct is None:
            track = (f"[green]{on * filled}[/green]"
                     f"[dim]{off * (width - filled)}[/dim]")
        else:
            no_work = max(0, min(width - filled, int(round(gray_pct / 100 * width))))
            track = (f"[green]{on * filled}[/green]"
                     f"[gray]{gray_glyph * no_work}[/gray]"
                     f"[dim]{off * (width - filled - no_work)}[/dim]")
        return (f"{label} {pct:5.1f}%  {track}  "
                f"{bar.get('done', 0):,} / {bar.get('total', 0):,}")

    @staticmethod
    def _coverage_block(scope: dict) -> list[str]:
        """The bar rows for one scope, each subsidiary flush under its parent.

        The bars of a parent/subsidiary pair are emitted back to back with
        nothing between them -- no blank line, no explanation -- so the
        subordination is visible before any number is read. The explanations
        follow the pair's bars, so they can never open a gap inside it.
        """
        bars = scope.get("bars") or []
        if not bars:
            return []
        label_width = max(len(bucket.get("label") or bucket.get("key", "")) for bucket in bars)
        lines: list[str] = []
        index = 0
        while index < len(bars):
            bar = bars[index]
            group = [bar]
            if index + 1 < len(bars) and bars[index + 1].get("subsidiary"):
                group.append(bars[index + 1])
                index += 2
            else:
                index += 1
            lines += [StatsScreen._coverage_bar_line(member, label_width)
                      for member in group]
            # Child first, so the parent's own explanation reads last in the
            # block rather than between the parent and its subsidiary.
            for member in reversed(group):
                detail = member.get("detail")
                if detail:
                    lines.append(f"  [dim]{escape_markup(detail)}[/dim]")
        return lines

    @staticmethod
    def _format_coverage(coverage: dict) -> str:
        """Coverage over live items, at two scopes kept visibly separate.

        The first block is every live item. The second is the items the target
        AppIDs' enrichment filters select -- "what I care about" -- produced by
        the search builder's SQL translation of those filters, not by the
        daemon's per-item check. The note under the second block says which
        scope it is and, when the two coincide, why: an AppID with no filters,
        or one whose stored filters cannot be read, excludes nothing.

        The bars themselves are the metric's: it owns the labels, the counts and
        the reachable maximum, so this renderer only lays them out.
        """
        total = coverage.get("total", 0) or 0
        if not total:
            return "[dim]No live items to cover.[/dim]"

        lines = [f"[b]Live items:[/b] {total:,}", "", "[b]All live items[/b]"]
        lines += StatsScreen._coverage_block(coverage)

        filtered = coverage.get("filtered")
        if isinstance(filtered, dict):
            f_total = filtered.get("total", 0) or 0
            lines += ["", "[b]Target AppIDs' enrichment filters — what I care about[/b]"]
            if f_total:
                lines += StatsScreen._coverage_block(filtered)
            else:
                lines.append("  [dim]No live items match the filters.[/dim]")
            lines.append(f"  [dim]{StatsScreen._coverage_scope_note(filtered)}[/dim]")
        return "\n".join(lines)

    @staticmethod
    def _coverage_scope_note(filtered: dict) -> str:
        """Why the two coverage figures are what they are, in one sentence."""
        appids = filtered.get("appids") or []
        unreadable = filtered.get("unreadable") or []
        restricting = filtered.get("restricting") or []
        names = ", ".join(str(a) for a in appids) or "none configured"
        if unreadable:
            return (f"Target AppIDs: {names}. The stored filter set for "
                    f"{', '.join(str(a) for a in unreadable)} could not be read, "
                    "so those items are all counted (no exclusion).")
        if not filtered.get("with_filters"):
            return (f"Target AppIDs: {names}. No enrichment filters are set for "
                    "them, so both figures are the same.")
        if not restricting:
            return (f"Target AppIDs: {names}. Their filters exclude nothing "
                    "(a percentile has no fixed predicate), so both figures are "
                    "the same.")
        return (f"Target AppIDs: {names}. SQL translation of their filters; it may "
                "differ from the daemon's per-item check on translated (_en) fields.")

    @staticmethod
    def _format_duration(seconds) -> str:
        """A span in one unit, coarse enough to read: ``53d``, ``4h``, ``12m``.

        The ETA is never split into days-and-hours: one form only, so the
        uncertainty beside it is the only second number the reader parses.
        """
        seconds = max(0.0, float(seconds or 0))
        if seconds >= 86400:
            return f"{seconds / 86400:,.0f}d"
        if seconds >= 3600:
            return f"{seconds / 3600:,.0f}h"
        if seconds >= 60:
            return f"{seconds / 60:,.0f}m"
        return f"{seconds:,.0f}s"

    @staticmethod
    def _format_uncertainty(pct) -> str:
        """The relative uncertainty, always as a percentage."""
        if pct is None:
            return ""
        if pct < 1:
            return "± <1%"
        return f"± {pct:,.0f}%"

    @staticmethod
    def _format_queue_eta(value: dict) -> str:
        """Outstanding depth, rate and time to drain for each work queue.

        The rate is in active time, so a pause does not read as a slowdown; the
        header says how much of the window was paused and how much API inflow
        was subtracted. A queue with no completions in the window shows "no rate
        yet" rather than a fabricated number, and one with nothing outstanding
        shows "drained". Where the figure is gross -- the three queues whose
        inflow nobody records -- the row says so, because a gross rate must not
        be read as a time to empty. See `docs/data-pipeline.md`.
        """
        queues = value.get("queues") or {}
        rows = (
            ("api", "API fetch"),
            ("web", "Web scrape"),
            ("image", "Image"),
            ("translation", "Translation"),
        )
        lines = [
            f"[b]Rate window:[/b] {StatsScreen._format_duration(value.get('window_seconds'))}"
            f"   [dim]paused {StatsScreen._format_duration(value.get('paused_seconds'))};"
            f" API inflow subtracted: {value.get('sweep_inflow', 0):,}[/dim]",
            "",
        ]
        for key, label in rows:
            entry = queues.get(key)
            if not isinstance(entry, dict):
                continue
            outstanding = entry.get("outstanding", 0) or 0
            eta = entry.get("eta_seconds")
            if outstanding <= 0:
                rate = "[dim]—[/dim]"
                drain = "[green]drained[/green]"
            elif eta is None:
                rate = "[dim]—[/dim]"
                drain = "[dim]no rate yet[/dim]"
            else:
                basis = "" if entry.get("basis") == "net" else " [dim](gross)[/dim]"
                rate = f"{entry.get('per_day', 0):,.1f}/day"
                drain = (f"{StatsScreen._format_duration(eta)} "
                         f"{StatsScreen._format_uncertainty(entry.get('uncertainty_pct'))}{basis}")
            lines.append(
                f"{label:<12} {outstanding:>10,} outstanding   {rate:>10}   {drain}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_dead_items_by_queue(stuck: dict) -> str:
        """Dead items still sitting in a queue, called out rather than hidden."""
        labels = (
            ("web", "Web scrape"),
            ("image", "Image"),
            ("translation", "Translation"),
            ("api", "API fetch"),
        )
        total = sum(stuck.get(key, 0) or 0 for key, _ in labels)
        if not total:
            return "[green]No dead items are still sitting in a queue.[/green]"
        lines = [
            f"[bold red]{total:,} dead item(s) are still sitting in a work queue[/bold red]",
            "",
        ]
        for key, label in labels:
            lines.append(f"  {label}: {stuck.get(key, 0) or 0:,}")
        # The metric module owns the sentence, so this screen and the web panel
        # say the same thing about the same figure (issue 74).
        lines.append(f"\n[dim]{metrics.DEAD_QUEUED_MEANING}[/dim]")
        return "\n".join(lines)

    @staticmethod
    def _format_handoff_metric(value, zero_text: str, bad_text: str) -> str:
        """A handoff counter, drawn as an all-clear when it reads zero.

        Both counters are meant to be zero, so a green sentence is more useful
        than the digit 0: it says the invariant held, not merely that a query
        returned nothing.
        """
        if not value:
            return f"[green]{zero_text}[/green]"
        return f"[bold red]{value:,} {bad_text}[/bold red]"

    @staticmethod
    def _format_priority(breakdowns: dict) -> str:
        """Outstanding work per queue, read as queue state rather than a column dump."""
        labels = {
            "translation_priority": "Translation",
            "image_priority": "Image",
            "web_scrape_priority": "Web scrape",
        }
        lines = []
        for key, label in labels.items():
            rows = breakdowns.get(key, [])
            total = sum(row.get("cnt", 0) for row in rows)
            lines.append(f"[b]{label} queue:[/b] {total:,} waiting")
            for row in rows:
                lines.append(f"  priority {row.get('prio')}: {row.get('cnt', 0):,}")
            lines.append("")
        return "\n".join(lines).rstrip() or "[dim]No data.[/dim]"


class AnalysisScreen(Screen):
    """Screen that analyzes the view window for Steam Workshop items."""

    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = db_path
        self.bucket_days = 7

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="analysis-main"):
            with Vertical(id="analysis-sidebar"):
                yield Label("[b]View Window Analysis[/b]", id="analysis-title")
                yield Label("Bucket size\n(days):", id="analysis-bucket-label")
                yield Input(value="7", id="analysis-bucket-size")
                yield Button("Recalculate", id="btn-analysis-recalc")
                yield Button("Close", id="btn-analysis-close", variant="error")
            with Vertical(id="analysis-right"):
                yield Static(id="analysis-summary")
                with VerticalScroll(id="analysis-table-scroll"):
                    yield DataTable(id="analysis-table")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#analysis-bucket-size").styles.width = 10
        self.query_one("#analysis-sidebar").styles.width = 22
        self.run_analysis()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-analysis-recalc":
            self.run_analysis()
        elif event.button.id == "btn-analysis-close":
            self.app.pop_screen()

    def run_analysis(self) -> None:
        try:
            bucket_input = self.query_one("#analysis-bucket-size", Input).value
            self.bucket_days = max(1, int(bucket_input or "7"))
        except ValueError:
            self.bucket_days = 7

        result = view_window_analysis(self.db_path, bucket_days=self.bucket_days)
        table = self.query_one("#analysis-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Age Range", "Items", "Median Views", "P10", "P90", "Relative")

        max_median = max((bucket["median"] for bucket in result["buckets"]), default=1)

        for bucket in result["buckets"]:
            bar_len = int(bucket["median"] / max(max_median, 1) * 40)
            bar = "█" * bar_len
            table.add_row(
                f"{bucket['age_start']}-{bucket['age_end']}d",
                str(bucket["count"]),
                str(bucket["median"]),
                str(bucket["p10"]),
                str(bucket["p90"]),
                f"[white]{bar}[/white]",
            )

        if result["estimated_window_days"]:
            summary = (f"[b]Estimated view window: ~{result['estimated_window_days']} days[/b]  "
                       f"(analyzed {result['items_analyzed']:,} items, "
                       f"{len(result['buckets'])} buckets)")
        else:
            summary = (f"Insufficient data to estimate view window. "
                       f"({result['items_analyzed']:,} items analyzed, "
                       f"{len(result['buckets'])} buckets)")
        self.query_one("#analysis-summary", Static).update(summary)


class DaemonManagerScreen(Screen):
    """Screen to manage the background daemon process."""

    def __init__(self, controller: DaemonController):
        super().__init__()
        self.controller = controller
        # Byte offset of the last line shown; each poll resumes here so it only
        # asks for what has appeared since.
        self._log_offset = 0
        self._log_timer = None
        # A transition holds the controller for as long as the process takes to
        # stop, so only one may be in flight and the controls are disabled while
        # it runs.
        self._transitioning = False
        # A rotation hands its compression to a background thread; the button is
        # disabled from the controller's own status on every tick, so a second
        # press cannot start a second rotation.
        self._rotating = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="dm-main"):
            with Vertical(id="dm-controls", classes="dm-panel"):
                yield Label("[b]Daemon Manager[/b]", id="dm-title")
                yield Static(id="dm-status")
                yield Button("Start", id="dm-start", variant="success")
                yield Button("Stop", id="dm-stop", variant="error")
                yield Button("Restart", id="dm-restart", variant="warning")
                yield Button("Close", id="dm-close")
            with Vertical(id="dm-log", classes="dm-panel"):
                yield Label("[b]Log Output[/b]")
                # The size readout and the manual rotation control sit together,
                # above the log itself. Small and quiet: a status line, the
                # button, and the last rotation's outcome.
                yield Static(id="dm-log-size")
                yield Button(log_rotation.ROTATE_BUTTON_LABEL, id="dm-rotate")
                yield Static(id="dm-log-message", markup=False)
                yield RichLog(id="dm-log-view", auto_scroll=True, wrap=True, max_lines=200)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#dm-controls").styles.width = 20
        # Fill the pane on first paint, then keep polling. The old `tail -f`
        # subprocess is gone: it read the log a line at a time, which is too slow
        # for a production log. `tail_log` reads at most TAIL_BYTES per call, the
        # same bounded call the web panel makes.
        self._tick()
        self._log_timer = self.set_interval(2.0, self._tick)

    def on_unmount(self) -> None:
        # The screen is gone; stop asking the controller for log lines.
        if self._log_timer is not None:
            self._log_timer.stop()
            self._log_timer = None

    def _tick(self) -> None:
        """One two-second refresh: status, log readout, and the tail."""
        self._update_status()
        self._refresh_log_info()
        self._poll_tail()

    def _update_status(self) -> None:
        status = self.controller.status()
        if status["running"]:
            self.query_one("#dm-status", Static).update(f"[green]Running (PID: {status['pid']})[/green]")
        else:
            self.query_one("#dm-status", Static).update("[red]Not running[/red]")

    def _refresh_log_info(self) -> None:
        """Draw the shared readout and the last rotation's outcome.

        The strings come from ``src/log_rotation.py`` -- the same ones the web
        panel's poll renders -- so the two front ends show the same number and the
        same words, and the button's enabled state follows a rotation started in
        either process.
        """
        try:
            info = self.controller.log_status()
        # A poll failure must not take the screen down; the next tick tries again.
        except Exception as exc:
            logging.debug("Daemon log status failed: %s", exc)
            return
        try:
            self.query_one("#dm-log-size", Static).update(info.get("log_readout", ""))
            self.query_one("#dm-log-message", Static).update(
                info.get("rotation_message") or "")
            self.query_one("#dm-rotate", Button).disabled = (
                bool(info.get("rotating")) or not info.get("can_rotate"))
        except Exception:
            # The screen can be torn down mid-tick.
            return

    def _begin_rotation(self) -> None:
        """Rotate on a worker: the rename is fast but the filesystem can block."""
        if self._rotating:
            return
        self._rotating = True
        self.query_one("#dm-rotate", Button).disabled = True
        self.query_one("#dm-log-size", Static).update(log_rotation.ROTATING_LABEL)

        def work():
            try:
                result = self.controller.rotate_log()
            except Exception as exc:
                result = {"ok": False, "started": False,
                          "message": f"Rotation failed: {exc}"}
            try:
                self.app.call_from_thread(self._rotation_started, result)
            except RuntimeError:
                logging.debug("Rotation finished with no app to report to")

        self.run_worker(work, name="daemon-rotate", group="daemon",
                        thread=True, exclusive=True, exit_on_error=False)

    def _rotation_started(self, result: dict) -> None:
        """Report an outcome that will not appear in the polled status line."""
        self._rotating = False
        if result.get("message") and not result.get("started"):
            self.app.notify(result["message"],
                            severity="information" if result.get("ok") else "warning")
        self._refresh_log_info()

    def _set_controls_enabled(self, enabled: bool) -> None:
        for button_id in ("dm-start", "dm-stop", "dm-restart"):
            try:
                self.query_one(f"#{button_id}", Button).disabled = not enabled
            except Exception:
                # The screen can be torn down mid-transition; a control that is
                # already gone needs nothing done to it.
                logging.debug("Daemon control %s is not on screen", button_id)

    def _begin_transition(self, action_label: str, transition) -> None:
        """Run a daemon transition on a worker so the interface keeps running.

        Every controller transition blocks: ``stop`` polls the process every half
        second for up to ``STOP_TIMEOUT_SECONDS`` (220 s, derived from the daemon's
        worst case including the closing database snapshot, in
        ``src/daemon_control.py``) and then waits up to another 3 s
        for a forced kill, and ``restart`` is ``stop`` followed by ``start``.
        Called straight from the button handler, that froze the whole application
        for the duration -- no keypress, no screen change and no timer, including
        this screen's own two-second log poll, which fell silent at the moment
        its output was most wanted. See issue 26 in docs/code-issues.md.
        """
        if self._transitioning:
            # The controls are disabled while this is true, so reaching here
            # means a press that raced the disable; ignore it rather than
            # starting a second shutdown.
            return
        self._transitioning = True
        self._set_controls_enabled(False)
        self.query_one("#dm-status", Static).update(f"[yellow]{action_label}...[/yellow]")

        def work():
            try:
                changed, message = transition()
            except Exception as exc:
                # A controller fault must not leave the screen with its controls
                # disabled and no way back.
                changed, message = False, f"{action_label} failed: {exc}"
            try:
                self.app.call_from_thread(self._transition_finished, changed, message)
            except RuntimeError:
                # The app stopped while the transition was in flight; there is no
                # UI left to report to, and the transition itself happened.
                logging.debug("Daemon %s finished with no app to report to", action_label)

        self.run_worker(work, name=f"daemon-{action_label.lower()}", group="daemon",
                        thread=True, exclusive=True, exit_on_error=False)

    def _transition_finished(self, changed: bool, message: str) -> None:
        """Put the controls back, on the event loop, once the worker is done."""
        self._transitioning = False
        self._set_controls_enabled(True)
        self._update_status()
        if message and not changed:
            self.app.notify(message, severity="warning")

    def _poll_tail(self) -> None:
        """Write a bounded preview of the daemon log into the pane."""
        try:
            result = self.controller.tail_log(self._log_offset)
        # A poll failure must not take the whole manager screen down; the next
        # tick tries again.
        except Exception as exc:
            logging.debug("Daemon log poll failed: %s", exc)
            return
        view = self.query_one("#dm-log-view", RichLog)
        if result.get("reset"):
            # The controller jumped to the tail (first poll, rotation/truncation,
            # or this pane fell further behind than it is willing to send), so the
            # new lines do not join up with what is on screen. Drop the stale text
            # rather than rendering a view with an invisible gap in it.
            view.clear()
        for line in result.get("lines", []):
            # RichLog parses no markup (`markup=False`), so the daemon's own
            # lines -- including the titles it logs -- pass through literally.
            view.write(line)
        self._log_offset = result.get("offset", self._log_offset)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Every transition goes to a worker: these calls block for as long as the
        # daemon takes to stop, and running one here froze the interface until it
        # returned.
        if event.button.id == "dm-start":
            self._begin_transition("Starting", self.controller.start)
        elif event.button.id == "dm-stop":
            self._begin_transition("Stopping", self.controller.stop)
        elif event.button.id == "dm-restart":
            # One call rather than stop-then-start, so a restart cannot interleave
            # with another press between the two halves.
            self._begin_transition("Restarting", self.controller.restart)
        elif event.button.id == "dm-rotate":
            # Manual only: no timer and no size trigger calls this. It renames the
            # live log at once and compresses the archive off the event loop.
            self._begin_rotation()
        elif event.button.id == "dm-close":
            self.app.pop_screen()


class _EstimateBasis(NamedTuple):
    """One redraw's shared inputs for the row estimates.

    ``_render_rows`` builds this once and threads it through every row, so a
    redraw of N rows reads the ``web_delay`` state section once and walks the
    observed durations once. A direct call to an estimate method with no basis
    builds its own, which is what keeps the mid-pass throttle check honest: the
    delay is read fresh rather than reused from an earlier redraw.
    """
    # The running mean the waiting rows are priced at, built from the
    # delay-derived seed and the observed durations.
    item_seconds: float
    # Total seconds already spent on the items that have finished.
    spent_on_finished: float


class SubscriptionQueueScreen(ModalScreen):
    """The subscription queue: the engine subscribes or unsubscribes each item.

    The screen used to draw a clickable Steam URL per item and leave the actual
    subscribe to a human with a browser. It now runs each item through
    ``src.subscribe_engine``, which reads the item page's own subscribe button
    and follows the row's derived direction: an addition sends the subscribe POST
    only when the button says the item is not subscribed; a removal sends the
    unsubscribe POST only when the button says it is. The subscription is
    recorded from the POST's own answer -- the confirmation read is retired by
    default, and setting ``VERIFY_AFTER_SUBSCRIBE = True`` in the engine restores
    it for additions. There are no browser tabs and no URLs to click; per-item
    progress and failures are shown in place, and a settled item leaves the queue
    through the engine's own write (``mark_own_subscribed`` for an addition,
    ``mark_own_unsubscribed`` for a removal).

    The ``.pauselock`` is created on mount and removed on unmount, so the daemon
    stays quiet while the queue is open; the pass itself takes and releases the
    same lock (``subscribe_engine.run_subscription_pass``), which is what covers
    a caller that runs the engine outside this screen.

    The estimate *starts* from the persisted web delay: on the default path an
    item costs the engine **one gated page read** -- the pre-read that guards the
    POST; the retired confirmation read would make it a second when the switch is
    on -- so the initial per-item guess is that many times the shared persisted
    delay (the POST is an XHR and pays no interval). From then on it is nudged by
    what the pass has actually done: the wall-clock cost of each finished item is
    timed between the per-item results the pass already delivers, and the rows
    still waiting are priced from the running mean of those durations, seeded
    with the delay-derived guess so the first item moves it most. It is an
    estimate, not a promise, because the pass can also be refused, throttled or
    cancelled after it is drawn. See ``docs/tui.md`` and, for why this differs
    from the web overlay's countdown, ``docs/web-ui.md``.
    """

    # Four ticks a second, the web overlay's cadence.
    _ESTIMATE_TICK_SECONDS = 0.25
    # The word a row carries while the engine is reading it, in place of a
    # countdown: the item being processed is visibly not one still waiting. The
    # queue has two directions, so the word follows the row's own state -- a
    # queued removal must not read "subscribing...".
    _CURRENT_LABEL = "subscribing..."
    _CURRENT_REMOVE_LABEL = "unsubscribing..."

    def __init__(self, db_path: str, pause_lock_file: str, config: dict | None = None):
        super().__init__()
        self.db_path = db_path
        self.pause_lock_file = pause_lock_file
        self.config = config or {}
        self._items: list[dict] = []
        self._pass_running = False
        # workshop_id -> SubscribeOutcome, in the order the pass reports them.
        self._outcomes: dict[int, subscribe_engine.SubscribeOutcome] = {}
        self._pass_started_at: float | None = None
        # Wall-clock cost of each item the pass has finished, in the order it
        # reported them. The running mean is seeded with the delay-derived guess.
        self._observed_seconds: list[float] = []
        self._last_result_at: float | None = None
        self._estimate_timer = None
        # Set on unmount so a pass that is mid-wait stops waiting for the
        # screen it can no longer draw to.
        self._closing = False

    def on_mount(self) -> None:
        """Create the pause lock file when the screen is mounted.

        The lock's interval is recorded in the daemon state file beside the
        database, so the drain estimate measures the queues' active time rather
        than the wall clock; see ``src/activity.py``.
        """
        activity.begin_pause(self.pause_lock_file, self.db_path,
                             source="tui_subscription_queue")

    def on_unmount(self) -> None:
        """Remove the pause lock file when the screen is unmounted."""
        self._closing = True
        self._stop_estimate_timer()
        activity.end_pause(self.pause_lock_file, self.db_path)

    @staticmethod
    def _row_text(item: dict, status: str | None = None, colour: str | None = None,
                  countdown: str | None = None):
        """One item's line, built as Rich text so a Steam title stays literal.

        The marker is the item's real state from ``src/subscription.py`` -- the
        same table the list row, the detail pane and the web render from -- so a
        completed subscribe moves this row's glyph too, rather than every row
        drawing the green ``queued`` outline whatever happened. ``countdown`` is
        the estimated seconds until the engine reaches the item, absent for the
        item it is reading now and for outcomes already reported.
        """
        from rich.text import Text as RichText
        state = subscription.subscription_state(item)
        line = RichText()
        line.append(f"{subscription.glyph(state)} ", style=subscription.colour(state))
        line.append(f"#{item['workshop_id']}  ")
        line.append(str(item.get("title") or "Untitled"))
        if countdown:
            line.append("  ")
            line.append(countdown, style="dim")
        if status:
            line.append("  ")
            line.append(status, style=colour or "white")
        return line

    def _seed_item_seconds(self) -> float:
        """The initial per-item guess: one gated page read by default.

        On the default path the engine reads the item page once, before the
        POST; the confirmation read is behind
        :data:`subscribe_engine.VERIFY_AFTER_SUBSCRIBE`, so when it is on each
        item pays a second gated read. The guess follows that count, and every
        gated read waits the shared adaptive web interval, so the starting
        figure is the persisted ``web_delay`` state section times one or two.
        The delay is read fresh from the same owner every time
        (``src.web_worker.configured_web_delay``) rather than snapshotted, so a
        throttle doubling the engine writes into that section mid-pass moves the
        estimate on the next redraw.
        """
        reads = 2 if subscribe_engine.VERIFY_AFTER_SUBSCRIBE else 1
        return reads * configured_web_delay(self.config)

    def _estimate_basis(self) -> _EstimateBasis:
        """One redraw's shared estimate inputs: one delay read, one observed sum.

        ``_render_rows`` builds this once and passes it to every row, so a
        redraw of N rows neither re-reads the state file N times nor re-sums the
        observed durations N times. A direct estimate call builds its own.
        """
        seed = self._seed_item_seconds()
        observed = self._observed_seconds
        spent_on_finished = sum(observed)
        if not observed:
            item_seconds = seed
        else:
            item_seconds = (seed + spent_on_finished) / (len(observed) + 1)
        return _EstimateBasis(item_seconds, spent_on_finished)

    def _estimated_item_seconds(self, basis: _EstimateBasis | None = None) -> float:
        """The mean cost of a finished item, seeded with the delay-derived guess.

        One running mean over every item the pass has reported, as
        ``(seed + sum(observed)) / (1 + count)``: the seed is one virtual
        observation, so the first real item moves the estimate a lot and later
        ones less, and the delay-derived guess still pulls on it. A rolling
        window, a rate learned from past passes or anything else persisted is
        deliberately not used -- the pane is transient, and the items of the pass
        in front of it are the only evidence worth pricing the rest of it with.

        ``basis`` is the redraw's shared inputs when a caller already has them;
        with none, the delay is read fresh and the mean built from it.
        """
        if basis is None:
            basis = self._estimate_basis()
        return basis.item_seconds

    def _estimate_remaining(self, index: int, elapsed: float,
                            basis: _EstimateBasis | None = None) -> int:
        """Whole seconds until item ``index`` is reached, never negative.

        ``index`` is the row's position in the queue and ``elapsed`` is the
        wall-clock time since the pass started. The time already spent on the
        item now being processed is what is left of ``elapsed`` once the
        finished items' observed costs come out of it; every item still ahead of
        the row costs the running mean. Rounded up so an estimate is never drawn
        as "0s" while the item has not started.

        ``basis`` is ``_render_rows``' one-per-redraw inputs; with none -- a
        direct call -- the delay and the observed durations are read fresh.
        """
        if basis is None:
            basis = self._estimate_basis()
        waiting_before = max(0, index - len(self._outcomes))
        spent_on_current = max(0.0, elapsed - basis.spent_on_finished)
        remaining = waiting_before * basis.item_seconds - spent_on_current
        return max(0, math.ceil(remaining))

    def _row_display(self, index: int, elapsed: float,
                     basis: _EstimateBasis | None = None):
        """``(countdown, status, colour)`` for the row at ``index``.

        The engine takes the queue in order, so the item it is reading now is
        the one after the outcomes already reported. That row carries the
        ``subscribing...`` or ``unsubscribing...`` word instead of a countdown,
        chosen from the row's own derived direction, which is what makes it
        distinct from the rows still waiting; a reported outcome keeps its status
        word and drops the countdown. A settled outcome is green whether it was
        an addition or a removal -- both left the queue by being carried out --
        and an outcome that stays queued (throttled, refused, a disagreement) is
        yellow. ``basis`` is the redraw's shared estimate inputs; with none, the
        countdown is read fresh.
        """
        item = self._items[index]
        outcome = self._outcomes.get(item["workshop_id"])
        if outcome is not None:
            colour = "yellow" if outcome.stays_queued else "green"
            return None, subscribe_engine.status_label(outcome.status), colour
        if not self._pass_running:
            return None, None, None
        if index == len(self._outcomes):
            label = (self._CURRENT_REMOVE_LABEL
                     if subscription.subscription_state(item) == subscription.QUEUED_REMOVE
                     else self._CURRENT_LABEL)
            return None, label, "cyan"
        return f"~{self._estimate_remaining(index, elapsed, basis)}s", None, None

    def _render_rows(self) -> None:
        """Redraw every row from the current outcomes and the estimate.

        Estimate times are not promises, so this only ever runs while the pass
        is live (moved by ``_tick_estimates``) or when an outcome lands; a
        finished screen has no countdown left on it. The estimate inputs are
        built once for the whole redraw -- one read of the shared delay and one
        sum of the observed durations -- rather than once per row.
        """
        if not self.is_mounted:
            return
        elapsed = 0.0
        if self._pass_started_at is not None:
            elapsed = max(0.0, time.monotonic() - self._pass_started_at)
        basis = self._estimate_basis()
        for index, item in enumerate(self._items):
            countdown, status, colour = self._row_display(index, elapsed, basis)
            try:
                row = self.query_one(f"#sub-queue-item-{item['workshop_id']}", Static)
            except Exception:
                continue
            row.update(self._row_text(item, status=status, colour=colour,
                                      countdown=countdown))

    def _start_estimate_timer(self) -> None:
        if self._estimate_timer is None:
            self._estimate_timer = self.set_interval(
                self._ESTIMATE_TICK_SECONDS, self._tick_estimates)

    def _stop_estimate_timer(self) -> None:
        if self._estimate_timer is not None:
            self._estimate_timer.stop()
            self._estimate_timer = None

    def _tick_estimates(self) -> None:
        """Move every waiting row's estimate; stop once the pass is over."""
        if not self._pass_running:
            self._stop_estimate_timer()
            self._render_rows()
            return
        self._render_rows()

    def compose(self) -> ComposeResult:
        self._items = get_subscription_queue_items(self.db_path)
        with Vertical(id="subscription-queue-container"):
            yield Label("Subscription Queue", id="subscription-queue-title")
            if not self._items:
                yield Label(
                    "Queue is empty. Press 's' on an item to queue it for "
                    "subscription, or again on a subscribed item to queue a removal.")
            else:
                yield Static(
                    "Press Run Queue to run the queue through the engine. "
                    "Row times are estimates.",
                    id="subscription-queue-status",
                )
                for item in self._items:
                    yield Static(
                        self._row_text(item),
                        id=f"sub-queue-item-{item['workshop_id']}",
                    )
            # Neutral wording on purpose: the queue holds additions and
            # removals, so a single button cannot name the direction. The rows
            # and the pass tally carry it instead (the `s` footer binding's
            # description is fixed at class definition).
            yield Button(
                "Run Queue", id="btn-subscribe-queue", variant="primary",
                disabled=not self._items,
            )
            yield Button("Close", id="btn-close-subscription-queue")

    def _start_pass(self) -> None:
        """Run the queue through the engine on a worker thread."""
        if self._pass_running or not self._items:
            return
        self._pass_running = True
        self._outcomes = {}
        self._observed_seconds = []
        self._last_result_at = None
        self._pass_started_at = time.monotonic()
        self.query_one("#btn-subscribe-queue", Button).disabled = True
        self.query_one("#subscription-queue-status", Static).update(
            "Working the queue... (start times are estimates)")
        self._start_estimate_timer()
        self._render_rows()

        def work():
            return subscribe_engine.run_subscription_pass(
                list(self._items),
                config=self.config,
                db_path=self.db_path,
                pause_lock_file=self.pause_lock_file,
                on_result=self._deliver_result,
                keep_running=lambda: not self._closing,
            )

        self.run_worker(work, name="subscribe-queue", group="subscribe-queue",
                        thread=True, exit_on_error=False)

    def _item_seconds_since_last_result(self, now: float) -> float:
        """The wall-clock cost of the item that just finished, from its own clock.

        The pass calls its per-item callback synchronously right after each
        item's engine run returns (``run_subscription_pass``), so the gap
        between two callbacks is that item's whole cost -- both gated reads and
        the POST -- which is what the persisted delay alone does not price. The
        first item is measured from the pass's start. Clamped at zero so a clock
        that steps back cannot feed a negative weight to the mean.
        """
        started = self._last_result_at
        if started is None:
            started = self._pass_started_at
        self._last_result_at = now
        if started is None:
            return 0.0
        return max(0.0, now - started)

    def _deliver_result(self, outcome) -> None:
        """Hand one outcome to the UI thread from the worker.

        The item's duration is measured here, on the worker thread, at the
        moment the pass reports it -- not on the UI thread, whose queueing delay
        would be counted into the item.
        """
        observed = self._item_seconds_since_last_result(time.monotonic())
        try:
            self.app.call_from_thread(self._apply_result, outcome, observed)
        except RuntimeError:
            # The screen closed while the pass was in flight; there is nothing
            # left to draw, and the pass releases the pause on its own.
            logging.debug("[subscribe] dropped a result; the screen is gone")

    def _apply_result(self, outcome, observed_seconds: float) -> None:
        """Record one outcome, read the item's state back and redraw its row.

        ``observed_seconds`` is the item's measured cost, which the estimate's
        running mean is built from. The engine writes a confirmed subscription
        into the shared table before it returns the outcome
        (``mark_own_subscribed`` also clears the queue flag), so reading the item
        back is what tells this row whether its marker moved -- the row renders
        from the same ``src/subscription.py`` table the list, the detail pane and
        the web do. A read that fails leaves the row drawing its stored,
        still-queued state, which is the honest answer when the state cannot be
        read.
        """
        self._observed_seconds.append(observed_seconds)
        self._outcomes[outcome.workshop_id] = outcome
        states = get_subscription_states(self.db_path, [outcome.workshop_id])
        fresh = states.get(outcome.workshop_id)
        if fresh is not None:
            for item in self._items:
                if item["workshop_id"] == outcome.workshop_id:
                    for column in ("own_subscribed", "is_queued_for_subscription",
                                   "own_first_subscribed_at"):
                        item[column] = fresh.get(column)
                    break
        self._render_rows()
        # A row on the results list moves now rather than at the poll's next
        # tick; the poll is still what catches every other writer.
        refresh = getattr(self.app, "refresh_subscription_rows", None)
        if refresh is not None:
            self.app.call_after_refresh(refresh, [outcome.workshop_id])
        if not outcome.is_subscribed and outcome.message:
            logging.info("[subscribe] %s: %s", outcome.workshop_id, outcome.message)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Finish the pass: re-enable the button and report the tally."""
        if event.worker.group != "subscribe-queue":
            return
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            self._pass_running = False
            # The estimate is only for a live pass; drop it with the pass so a
            # finished screen shows outcomes rather than a stale "~3s".
            self._stop_estimate_timer()
            self._render_rows()
            if not self.is_mounted:
                return
            button = self.query_one("#btn-subscribe-queue", Button)
            button.disabled = not self._items
            outcomes = event.worker.result if event.state == WorkerState.SUCCESS else []
            subscribed = sum(1 for o in outcomes if o.is_subscribed)
            unsubscribed = sum(1 for o in outcomes if o.is_unsubscribed)
            remaining = len(outcomes) - subscribed - unsubscribed
            self.query_one("#subscription-queue-status", Static).update(
                f"Pass finished: {subscribed} subscribed, "
                f"{unsubscribed} unsubscribed, {remaining} left queued."
                if outcomes else "Pass finished with no results."
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-close-subscription-queue":
            self.app.pop_screen()
        elif event.button.id == "btn-subscribe-queue":
            self._start_pass()


def load_tui_state(path: str) -> dict:
    """Loads the TUI state from a YAML file."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return yaml.safe_load(handle) or {}
    except Exception:
        return {}

def save_tui_state(path: str, state: dict) -> None:
    """Saves the TUI state to a YAML file."""
    try:
        with open(path, 'w', encoding='utf-8') as handle:
            yaml.dump(state, handle, default_flow_style=False)
    except Exception:
        logging.debug("Failed to save TUI state")
        pass

import re

def bbcode_to_markdown(text: str) -> str:
    """Converts common Steam BBCode tags to Markdown."""
    if not text:
        return ""
    
    # 1. Headers
    text = re.sub(r'\[h1\](.*?)\[/h1\]', r'# \1', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[h2\](.*?)\[/h2\]', r'## \1', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[h3\](.*?)\[/h3\]', r'### \1', text, flags=re.IGNORECASE | re.DOTALL)

    # 2. Basic Formatting
    text = re.sub(r'\[b\](.*?)\[/b\]', r'**\1**', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[i\](.*?)\[/i\]', r'*\1*', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[u\](.*?)\[/u\]', r'\1', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[s\](.*?)\[/s\]', r'~~\1~~', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[strike\](.*?)\[/strike\]', r'~~\1~~', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[spoiler\](.*?)\[/spoiler\]', r'[SPOILER: \1]', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[noparse\](.*?)\[/noparse\]', r'`\1`', text, flags=re.IGNORECASE | re.DOTALL)

    # 3. Links, Images, Videos
    text = re.sub(r'\[url=(.*?)\](.*?)\[/url\]', r'[\2](\1)', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[url\](.*?)\[/url\]', r'[\1](\1)', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[img\](.*?)\[/img\]', r'![image](\1)', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[video\](.*?)\[/video\]', r'[Video](\1)', text, flags=re.IGNORECASE | re.DOTALL)

    # 4. Lists
    # Bulleted lists
    text = re.sub(r'\[list\]', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'\[/list\]', '\n', text, flags=re.IGNORECASE)
    # Numbered lists (simplifying olist to numbered list)
    text = re.sub(r'\[olist\]', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'\[/olist\]', '\n', text, flags=re.IGNORECASE)
    # List items: handle [*] item -> * item
    text = re.sub(r'\[\*\]\s*', '* ', text)

    # 5. Tables (Simple conversion - Markdown tables are limited)
    text = re.sub(r'\[table\]', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'\[/table\]', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'\[tr\]', '| ', text, flags=re.IGNORECASE)
    text = re.sub(r'\[/tr\]', ' |\n', text, flags=re.IGNORECASE)
    text = re.sub(r'\[th\]', ' **', text, flags=re.IGNORECASE)
    text = re.sub(r'\[/th\]', '** |', text, flags=re.IGNORECASE)
    text = re.sub(r'\[td\]', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\[/td\]', ' |', text, flags=re.IGNORECASE)

    # 6. Quotes and Code
    text = re.sub(r'\[quote=(.*?)\](.*?)\[/quote\]', r'> **\1 said:**\n> \2', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[quote\](.*?)\[/quote\]', r'> \1', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[code\](.*?)\[/code\]', r'```\n\1\n```', text, flags=re.IGNORECASE | re.DOTALL)

    # 7. HR
    text = re.sub(r'\[hr\]', '\n---\n', text, flags=re.IGNORECASE)
    
    # Cleanup: remove extra spaces resulting from some conversions
    text = re.sub(r'\|  \|', '|', text)
    
    return text

class DetailsPane(VerticalScroll):
    """A scrollable pane for viewing workshop item details."""
    workshop_id = reactive(None)
    item_data = reactive(None) # Detailed data fetched from DB
    show_translated = reactive(True)
    # The id this pane is subscribed to in the app's item-update registry, or
    # None. It is tracked separately from `workshop_id` so the watcher can
    # unsubscribe the item the pane is leaving.
    _subscribed_id = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="details-buttons-row"):
            with Horizontal(id="top-left-buttons"):
                yield Button("Show Original", id="btn-toggle-translation", classes="details-button")
                # Windows-only, like the `o` binding it duplicates. It is present
                # but disabled while the item is not green, so the affordance is
                # discoverable with the reason rather than invisible.
                if self._folder_service() is not None and self._folder_service().is_supported():
                    yield Button("Open Folder", id="btn-open-folder",
                                 classes="details-button", disabled=True)
            yield Button("Jump to Author", id="btn-jump-author", variant="primary")

        with Horizontal(id="title-creator-row"):
            # The subscription marker sits immediately before the title, the same
            # convention the web detail pane uses. It replaces the old
            # btn-queue-sub/btn-unqueue-sub pair: the marker *is* the control, and
            # two indicators of one flag is what this removed.
            yield Label("", id="item-sub-marker")
            yield Label("", id="item-title")
            yield Label("", id="item-creator")

        # The sticky first-seen-subscribed stamp, beside the marker that reads
        # from the same column. Hidden entirely when it is NULL: an item never
        # seen subscribed must not gain a dated line implying an observation
        # nobody made.
        yield Label("", id="item-sub-at")
        
        yield Label("", id="wilson-scores")
        yield Static(classes="blank-line")
            
        with Horizontal(id="stats-row"):
            with Vertical(classes="stats-col"):
                yield Label("ID: N/A", id="stat-id")
                yield Label("Created: N/A", id="stat-created")
                yield Label("Updated: N/A", id="stat-updated")
                yield Label("Tags: N/A", id="stat-tags")
            with Vertical(classes="stats-col"):
                yield Label("Size: N/A", id="stat-size")
                yield Label("Views: N/A", id="stat-views")
                yield Label("Subscribers: N/A", id="stat-subscribers")
                yield Label("Favorites: N/A", id="stat-favorites")

        # The translation notice is its own element rather than a blockquote in
        # the markdown: `Markdown` renders Rich tags literally, and the web
        # pane draws the same sentence as its own `.translation-notice`
        # paragraph. A `Label` does interpret Textual markup, so the notice can
        # keep the web's italic, muted emphasis. It stays above the description,
        # where the blockquote sat.
        desc_container = Vertical(
            Label("", id="translation-notice"),
            Markdown(id="detail-content"),
            id="desc-container"
        )
        desc_container.border_title = "Description"
        yield desc_container

    def on_unmount(self) -> None:
        self._unsubscribe_from_item()

    def _folder_service(self):
        """The app's folder helper, or None in a bare pane (markup tests)."""
        return getattr(self.app, "workshop_folders", None)

    def _registry(self):
        """The app's item-update registry, or None in a bare pane."""
        return getattr(self.app, "item_updates", None)

    def _subscribe_to_item(self, workshop_id) -> None:
        """Join the registry while this pane displays ``workshop_id``."""
        self._unsubscribe_from_item()
        registry = self._registry()
        if registry is not None and registry.subscribe(workshop_id, self):
            self._subscribed_id = int(workshop_id)

    def _unsubscribe_from_item(self) -> None:
        registry = self._registry()
        if registry is not None and self._subscribed_id is not None:
            registry.unsubscribe(self._subscribed_id, self)
        self._subscribed_id = None

    def apply_item_update(self, item: dict) -> None:
        """The pane's one way to receive a change to the item it displays.

        A block is merged over what the pane already has rather than replacing
        it, so a producer that carried only the marker columns cannot blank the
        description, and a producer that carried the whole row fills everything
        the pane draws. Any field the pane renders is therefore refreshed by
        whichever update arrives, without this pane being a target its caller
        has to remember.
        """
        if int(item.get("workshop_id", -1)) != int(self.workshop_id or -2):
            return
        merged = dict(self.item_data or {})
        merged.update(item)
        if merged != (self.item_data or {}):
            self.item_data = merged

    def _open_folder_button(self) -> Button | None:
        try:
            return self.query_one("#btn-open-folder", Button)
        except Exception:
            return None

    @db_poll.guard_db_poll("detail pane poll")
    async def refresh_data(self) -> None:
        """Read the adopted item and hand it to the app's dispatch point.

        The pane is one subscriber among however many display this id, so the
        read is not drawn by the reader: it is dispatched, exactly like a poll's
        or an action's update, and the pane redraws because it subscribed. A
        transient lock skips this read instead of ending the session, and
        ``item_data`` is left exactly as it was because the read did not happen.
        """
        if self.workshop_id:
            # We access db_path via self.app (ScraperApp instance)
            fresh_data = get_item_details(self.app.db_path, self.workshop_id)
            if fresh_data:
                dispatch = getattr(self.app, "dispatch_item_update", None)
                if dispatch is not None:
                    dispatch(fresh_data)
                else:
                    self.item_data = fresh_data

    async def watch_workshop_id(self, workshop_id: int) -> None:
        """When the pane adopts an item, subscribe and apply detail priority once.

        The bump lives here rather than in the list-view highlight handler so it
        fires on pane load, not on every highlight event. The pane subscribes
        before the read so the dispatch of the initial read reaches it, and the
        read stays read-only on purpose: re-applying detail priority there would
        re-queue whatever is on screen indefinitely.
        """
        self.item_data = None
        if workshop_id:
            self._subscribe_to_item(workshop_id)
            db_path = self.app.db_path
            raise_web_scrape_priority_for_detail(db_path, workshop_id)
            raise_translation_priority_for_detail(db_path, workshop_id)
            raise_image_priority_for_detail(db_path, workshop_id)
            raise_api_priority_for_detail(db_path, workshop_id)
            await self.refresh_data()
        else:
            self._unsubscribe_from_item()

    def watch_item_data(self, item_data: dict) -> None:
        self.update_content()

    def watch_show_translated(self, show_translated: bool) -> None:
        self.update_content()

    def update_content(self) -> None:
        if not self.item_data:
            self.query_one("#detail-content", Markdown).update("Select an item to see details.")
            self.query_one("#translation-notice", Label).update("")
            self.query_one("#translation-notice", Label).display = False
            self.query_one("#item-title", Label).update("")
            self.query_one("#item-sub-marker", Label).update("")
            self.query_one("#item-sub-marker", Label).display = False
            self.query_one("#item-sub-at", Label).update("")
            self.query_one("#item-sub-at", Label).display = False
            self.query_one("#item-creator", Label).update("")
            self.query_one("#btn-toggle-translation").display = False
            self.query_one("#btn-jump-author").display = False
            open_btn = self._open_folder_button()
            if open_btn is not None:
                open_btn.display = False
            
            for stat in ["id", "created", "updated", "tags", "size", "views", "subscribers", "favorites"]:
                self.query_one(f"#stat-{stat}", Label).display = False
            self.query_one("#wilson-scores", Label).update("")
            return

        item = self.item_data

        # The marker and its colour come from src/subscription.py, the same table
        # the web grid and pane render from.
        sub_state = subscription.subscription_state(item)
        sub_glyph, sub_colour, _css, _label = subscription.marker_spec(sub_state)
        sub_marker = self.query_one("#item-sub-marker", Label)
        sub_marker.update(f"[{sub_colour}]{sub_glyph}[/]")
        sub_marker.tooltip = subscription.tooltip(sub_state)
        sub_marker.display = True

        # The date the account was first seen subscribed, worded with the
        # marker's own "subscribed" vocabulary and formatted by the pane's
        # shared `format_ts`. There is no separate history to show, so an item
        # that has never been seen subscribed shows no line at all.
        sub_at = item.get("own_first_subscribed_at")
        sub_at_label = self.query_one("#item-sub-at", Label)
        if sub_at:
            sub_at_label.update(f"[b]Subscribed at:[/b] {format_ts(sub_at)}")
            sub_at_label.display = True
        else:
            sub_at_label.update("")
            sub_at_label.display = False

        # The folder control follows the marker: enabled only for the green
        # `downloaded` state, and disabled with the reason when it is not. The
        # keyboard path says the same thing through a notification.
        open_btn = self._open_folder_button()
        if open_btn is not None:
            open_btn.display = True
            is_downloaded = sub_state == subscription.DOWNLOADED
            open_btn.disabled = not is_downloaded
            open_btn.label = "Open Folder" if is_downloaded else "Open Folder (not downloaded)"
            open_btn.tooltip = (
                "Open this item's workshop folder in Explorer on the machine running "
                "the app." if is_downloaded else
                "Only a subscribed item Steam has downloaded can be opened."
            )
        
        display_translated = self.show_translated and item.get("translate_version")
        title = item.get("title_en") if display_translated and item.get("title_en") else item.get("title", "N/A")
        
        creator_name = item.get("personaname_en") if display_translated and item.get("personaname_en") else item.get("personaname")
        if not creator_name:
            creator_name = str(item.get("creator_steamid", "N/A"))
            
        self.query_one("#item-title", Label).update(f"[b]{escape_markup(title)}[/b]")
        self.query_one("#item-creator", Label).update(escape_markup(creator_name))
        
        jump_btn = self.query_one("#btn-jump-author", Button)
        if item.get("creator_steamid"):
            jump_btn.display = True
        else:
            jump_btn.display = False

        if display_translated:
            desc = item.get("extended_description_en") or item.get("short_description_en")
            if not desc:
                desc = item.get("extended_description") or item.get("short_description") or "N/A"
        else:
            desc = item.get("extended_description") or item.get("short_description") or "N/A"

        toggle_btn = self.query_one("#btn-toggle-translation")
        if item.get("translate_version"):
            toggle_btn.display = True
            toggle_btn.label = "Show Original" if self.show_translated else "Show Translation"
        else:
            toggle_btn.display = False

        tags_list = parse_tags(item.get("tags", "[]"))

        for stat in ["id", "created", "updated", "tags", "size", "views", "subscribers", "favorites"]:
            self.query_one(f"#stat-{stat}", Label).display = True

        self.query_one("#stat-id", Label).update(f"[b]ID:[/b] {item.get('workshop_id', 'N/A')}")
        self.query_one("#stat-created", Label).update(f"[b]Created:[/b] {format_ts(item.get('steam_created_at'))}")
        
        updated_ts = item.get('steam_updated_at')
        updated_str = format_ts(updated_ts) if updated_ts and updated_ts != item.get('steam_created_at') else "N/A"
        
        updated_label = self.query_one("#stat-updated", Label)
        if updated_str == "N/A":
            updated_label.display = False
        else:
            updated_label.display = True
            updated_label.update(f"[b]Updated:[/b] {updated_str}")
        
        tags_text = ", ".join(tags_list) if tags_list else "None"
        self.query_one("#stat-tags", Label).update(f"[b]Tags:[/b] {escape_markup(tags_text)}")
        self.query_one("#stat-size", Label).update(f"[b]Size:[/b] {format_size(item.get('file_size'))}")
        self.query_one("#stat-views", Label).update(f"[b]Views:[/b] {format_count(item.get('views', 0))}")
        
        subs_current = format_count(item.get('subscriptions', 0))
        subs_lifetime = format_count(item.get('lifetime_subscriptions', 0))
        self.query_one("#stat-subscribers", Label).update(f"[b]Subscribers:[/b] {subs_current} / {subs_lifetime}")
        
        favs_current = format_count(item.get('favorited', 0))
        favs_lifetime = format_count(item.get('lifetime_favorited', 0))
        self.query_one("#stat-favorites", Label).update(f"[b]Favorites:[/b] {favs_current} / {favs_lifetime}")

        wilson_label = self.query_one("#wilson-scores", Label)
        app = self.app
        cutoffs = getattr(app, '_wilson_cutoffs', {}) if app else {}
        wilson_label.update(self._format_wilson_scores(item, cutoffs))
        wilson_label.display = bool(item.get("wilson_favorite_score") is not None)

        md_content = bbcode_to_markdown(desc)
        self.query_one("#detail-content", Markdown).update(md_content)

        # The wording is shared with the page (`src/pending.py`); only the
        # styling is Textual markup, on a widget that interprets it. The notice
        # shows from the moment the item is queued and no translation is
        # stored -- the same test the web pane uses.
        notice = self.query_one("#translation-notice", Label)
        if item.get("translation_priority", 0) > 0 and not item.get("translate_version"):
            notice.update(f"[italic]{pending.TRANSLATION_REQUESTED_NOTICE}[/italic]")
            notice.display = True
        else:
            notice.update("")
            notice.display = False

    def _format_wilson_scores(self, item: dict, cutoffs: dict) -> str:
        """Formats Wilson scores with percentile-based coloring."""
        def colorize(label, score_key):
            score = item.get(score_key)
            if score is None:
                return f"[gray]{label}: N/A[/gray]"
            pct = score * 100
            p99 = cutoffs.get(score_key.replace("score", "p99"), 0) or 0
            p90 = cutoffs.get(score_key.replace("score", "p90"), 0) or 0
            p50 = cutoffs.get(score_key.replace("score", "p50"), 0) or 0
            if score >= p99:
                return f"[yellow]{label}: ! {pct:.1f}% ![/yellow]"
            if score >= p90:
                return f"[yellow]{label}: {pct:.1f}%[/yellow]"
            if score >= p50:
                return f"[white]{label}: {pct:.1f}%[/white]"
            return f"[gray]{label}: {pct:.1f}%[/gray]"
        fav = colorize("Favorite Score", "wilson_favorite_score")
        sub = colorize("Subscriber Score", "wilson_subscription_score")
        return f"{sub}   {fav}"

class WorkshopItem(ListItem):
    """A list item representing a workshop item."""
    BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    # One tick for the whole list; an item divides it by its stage's period
    # multiplier to get its own frame, so items can run at different speeds from
    # a single timer. The modulus keeps the sequence repeating exactly for the
    # slowest stage.
    _tick = 0
    _TICK_MODULUS = len(BRAILLE) * max(m for _s, m, _c, _l in pending.STAGES)

    def __init__(self, item_data: dict):
        super().__init__()
        self.item_data = item_data

    def _spinner(self) -> str:
        """This item's marker, or a space when nothing is outstanding.

        The speed says which stage is waiting and the colour fades with it, so a
        marker that clears in seconds does not look like one that may take
        hours. Both come from `src/pending.py`, which the web list mirrors.
        """
        stage = pending.pending_stage(self.item_data)
        if stage is None:
            return " "
        multiplier, colour = pending.stage_spec(stage)
        frame = (self._tick // multiplier) % len(self.BRAILLE)
        return f"[{colour}]{self.BRAILLE[frame]}[/]"

    def _subscription_marker(self) -> str:
        """This item's subscription marker as Textual markup.

        The glyph and colour come from `src/subscription.py`, the same table the
        web grid and detail pane render from, so the two front ends cannot
        disagree about why a row looks the way it does. Not clickable: there is
        no click affordance in the TUI yet, and the keyboard toggle stays the way
        to change it.
        """
        state = subscription.subscription_state(self.item_data)
        glyph, colour, _css, _label = subscription.marker_spec(state)
        return f"[{colour}]{glyph}[/]"

    def _title_markup(self) -> str:
        """The bold title markup, underlined while the item is ignored.

        Keyed off the stored status rather than off the action that set it: the
        item-update poll can report a row the owner ignored in another front end
        or a previous session, and the same path must draw the underline. Only
        markup this module writes itself is left unescaped; the Steam title goes
        through ``escape_markup`` first (see the module docstring).
        """
        title = self.item_data.get("title_en") or self.item_data.get("title", "Untitled")
        escaped = escape_markup(title)
        if self.item_data.get("fetch_status") == IGNORED_FETCH_STATUS:
            return f"[b][u]{escaped}[/u][/b]"
        return f"[b]{escaped}[/b]"

    def compose(self) -> ComposeResult:
        wid = self.item_data.get("workshop_id", "N/A")
        creator = self.item_data.get("personaname_en") or self.item_data.get("personaname") or self.item_data.get("creator_steamid", "Unknown Creator")
        spin = self._spinner()
        marker = self._subscription_marker()

        yield Label(f"{self._title_markup()} ({wid})")
        yield Label(f"  By: {escape_markup(creator)}   {spin} {marker}")

    async def refresh_item(self) -> None:
        """Re-compose the item to reflect any changes in item_data."""
        await self.recompose()

    def on_mount(self) -> None:
        """Subscribe this row to the item it is showing.

        The registry describes what is on screen, so a row joins it when it
        mounts and leaves it when it unmounts; a new search's ``clear()``
        therefore unsubscribes the whole previous result set without the search
        path knowing anything about the registry.
        """
        self._subscribe_to_item()

    def on_unmount(self) -> None:
        self._unsubscribe_from_item()

    def _subscribe_to_item(self) -> None:
        registry = getattr(self.app, "item_updates", None)
        if registry is not None:
            registry.subscribe(self.item_data.get("workshop_id"), self)

    def _unsubscribe_from_item(self) -> None:
        registry = getattr(self.app, "item_updates", None)
        if registry is not None:
            registry.unsubscribe(self.item_data.get("workshop_id"), self)

    def apply_item_update(self, item: dict) -> None:
        """The row's one way to receive a change to the item it displays.

        ``item`` is a block, not a field: whatever the producer had is merged
        over this row's data, so a block carrying only the subscription columns
        leaves the title and creator alone, and a block carrying a column this
        row does not draw yet is simply stored. Recomposing is skipped when
        nothing the row holds moved, which keeps the general poll free of
        needless redraws.
        """
        if int(item.get("workshop_id", -1)) != int(self.item_data.get("workshop_id", -2)):
            return
        changed = any(self.item_data.get(key) != value for key, value in item.items())
        self.item_data.update(item)
        if changed:
            self.refresh(recompose=True)


class SearchRow(Horizontal):
    """A single row in the search builder."""
    def __init__(self, fields: list[str], field_ops_map: dict, is_first: bool = False, initial_filter: dict = None):
        super().__init__(classes="search-row")
        self.fields = fields
        self.field_ops_map = field_ops_map  # {field_name: [op_names]}
        self.is_first = is_first
        self.initial_filter = initial_filter or {}
        # A restored filter whose value an enum control does not offer (a legacy
        # or API-written value) is kept as an extra option so the row displays
        # and round-trips it. It is never added to a fresh row's choices.
        init_field = self.initial_filter.get("field")
        init_value = self.initial_filter.get("value")
        self._restored_enum_value = (
            init_value if _FIELD_TYPES.get(init_field) == "enum"
            and isinstance(init_value, str) and init_value else None
        )
        self._enum_value = self._restored_enum_value
        self._extra_enum_values = {self._restored_enum_value} if self._restored_enum_value else set()

    def _ops_for_field(self, field: str) -> list[str]:
        return self.field_ops_map.get(field, ["contains", "does_not_contain"])

    def compose(self) -> ComposeResult:
        field = self.initial_filter.get("field", self.fields[0])
        field_options = [(f, f) for f in self.fields]
        yield Select(field_options, prompt="Field", id="field-select", classes="row-field", value=field)

        ops = self._ops_for_field(field)
        op_options = [(o.replace("_", " "), o) for o in ops]
        
        op = self.initial_filter.get("op", ops[0])
        if op not in ops:
            op = ops[0]
            
        yield Select(op_options, prompt="Op", id="op-select", classes="row-op", value=op)
        
        # Both value controls are mounted and one is hidden, rather than
        # swapping widgets on every field change: a mounted row keeps its id and
        # its place in the row's own layout. The enum control is a Select from
        # the schema's values, so `Subscribed` can only be one of them; the
        # free-text control stays an Input for every other field.
        val = self.initial_filter.get("value", "")
        yield Input(placeholder="Value", id="value-input", classes="row-input", value=val)
        enum_options = _enum_value_options(field, op) or [("—", "")]
        yield Select(enum_options, value=enum_options[0][1], id="value-select", classes="row-input")
        
        yield Button("AND", id="btn-and", variant="default", classes="row-btn")
        yield Button("OR", id="btn-or", variant="default", classes="row-btn")
        if not self.is_first:
            yield Button("X", id="btn-remove", variant="error", classes="row-btn-remove")
        else:
            # Placeholder to keep alignment
            yield Static("", classes="row-btn-remove")

    def on_mount(self) -> None:
        self._sync_value_control(restoring=True)

    def _sync_value_control(self, restoring: bool = False) -> None:
        """Shows the value control the current field/operator calls for.

        The enum control's choices depend on the operator as well as the field:
        `any` is dropped for `is_not`, so `is_not any` cannot be built. A value
        restored from a saved filter that is not among the choices is kept as an
        extra option, so a stored filter displays and round-trips instead of
        being silently rewritten; a value the user picked is dropped when the
        operator no longer offers it.
        """
        try:
            field = self.query_one("#field-select", Select).value
            op = self.query_one("#op-select", Select).value
            value_input = self.query_one("#value-input", Input)
            value_select = self.query_one("#value-select", Select)
        except Exception:
            return
        is_enum = _FIELD_TYPES.get(field) == "enum"
        value_input.display = not is_enum
        value_select.display = is_enum
        if not is_enum:
            return
        if self._restored_enum_value is not None:
            # A restored value wins until the mount-time sync has applied it,
            # even if a Change message beats on_mount to this handler.
            desired = self._restored_enum_value
            if restoring:
                self._restored_enum_value = None
        else:
            current = value_select.value
            desired = current if isinstance(current, str) and current else self._enum_value
        options = _enum_value_options(field, op)
        offered = [v for _, v in options]
        for extra in self._extra_enum_values:
            if extra not in offered:
                options.append((extra, extra))
                offered.append(extra)
        value_select.set_options(options)
        if desired is not None and desired in offered:
            value_select.value = desired
        elif offered:
            value_select.value = offered[0]
        self._enum_value = value_select.value if isinstance(value_select.value, str) else None

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "field-select":
            field = str(event.value)
            ops = self._ops_for_field(field)
            try:
                op_select = self.query_one("#op-select", Select)
                current_val = op_select.value
                
                # If we have an initial filter and op_select is uninitialized or blank, use it
                if current_val == Select.BLANK and self.initial_filter:
                    current_val = self.initial_filter.get("op", ops[0])
                    
                op_select.set_options([(o.replace("_", " "), o) for o in ops])
                
                if current_val in ops:
                    op_select.value = current_val
                else:
                    op_select.value = ops[0]
            except Exception:
                logging.debug("Widget not ready during mount")
                pass # Overlay might not be ready during initial mount
            self._sync_value_control()
        elif event.select.id == "op-select":
            self._sync_value_control()
        elif event.select.id == "value-select":
            self._enum_value = event.value if isinstance(event.value, str) else None

    def on_input_blurred(self, event: Input.Blurred) -> None:
        if event.control.id == "value-input":
            self._clamp_percentile()

    def _clamp_percentile(self) -> None:
        op_select = self.query_one("#op-select", Select)
        if op_select.value != "percentile":
            return
        try:
            value_input = self.query_one("#value-input", Input)
            v = int(float(value_input.value))
            v = max(0, min(99, v))
            value_input.value = str(v)
        # Empty or non-numeric user input is left exactly as typed; clamping happens
        # once it parses.
        except (ValueError, TypeError):
            pass

class SearchBuilder(VerticalScroll):
    """A container for multiple SearchRows."""
    def compose(self) -> ComposeResult:
        self.fields = ALL_FILTER_FIELDS
        # field_name -> [ops] lookup from the central schema
        self.field_ops_map = {f["field"]: f["ops"] for f in SEARCH_FILTER_SCHEMA}
        yield SearchRow(self.fields, self.field_ops_map, is_first=True)

    def add_row(self, logic: str) -> None:
        new_row = SearchRow(self.fields, self.field_ops_map)
        self.mount(new_row)
        new_row.logic = logic
        self._sync_overlay_state()

    def _sync_overlay_state(self) -> None:
        """Ask the app to re-apply the grey-out rule after a builder change.

        The overlay lives beside the sort controls, not in the builder, but its
        enabled state is decided by the builder's rows. Deferring to the next
        refresh lets the rows that were just mounted be composed before the rule
        reads them.
        """
        sync = getattr(self.app, "_sync_subscribed_overlay", None)
        if sync is not None:
            self.app.call_after_refresh(sync)

    def set_filters(self, filters: list[dict]) -> None:
        """Populates the builder with a given list of filters."""
        for row in list(self.query(SearchRow)):
            row.remove()
            
        if not filters:
            self.mount(SearchRow(self.fields, self.field_ops_map, is_first=True))
            self._sync_overlay_state()
            return

        for i, f in enumerate(filters):
            is_first = (i == 0)
            row = SearchRow(self.fields, self.field_ops_map, is_first=is_first, initial_filter=f)
            if not is_first:
                row.logic = f.get("logic", "AND")
            self.mount(row)
        self._sync_overlay_state()

    def get_filters(self) -> list[dict]:
        filters = []
        rows = self.query(SearchRow)
        for i, row in enumerate(rows):
            op = row.query_one("#op-select", Select).value
            field = row.query_one("#field-select", Select).value
            if not isinstance(field, str) or not isinstance(op, str) or not field.strip() or not op.strip():
                continue  # skip unconfigured rows (Sentinel.BLANK etc.)
            if _FIELD_TYPES.get(field) == "enum":
                val = row.query_one("#value-select", Select).value
                if not isinstance(val, str) or not val:
                    continue  # a blank enum control is an unconfigured row
            else:
                val = row.query_one("#value-input", Input).value
            if op == "percentile":
                try:
                    v = int(float(val))
                    v = max(0, min(99, v))
                    val = str(v)
                    row.query_one("#value-input", Input).value = val
                except (ValueError, TypeError):
                    val = "0"
                    row.query_one("#value-input", Input).value = val
            f = {
                "field": field,
                "op": op,
                "value": val,
            }
            if i > 0:
                f["logic"] = getattr(row, "logic", "AND")
            filters.append(f)
        return filters

class DatabaseCommands(Provider):
    """A command provider for database operations."""
    
    async def discover(self) -> Iterable[DiscoveryHit]:
        """Yield commands that should be discoverable when the palette opens."""
        yield DiscoveryHit(
            "Delete Never Fetched Items",
            self.app.action_delete_never_fetched_items,
            help="Delete items that were never successfully fetched",
        )
        yield DiscoveryHit(
            "Show Subscription Queue",
            self.app.action_show_subscription_queue,
            help="Subscribe each queued item through the engine",
        )

    async def search(self, query: str) -> Iterable[Hit]:
        """Search for database commands matching the query."""
        matcher = self.matcher(query)
        
        commands = {
            "Delete Never Fetched Items": self.app.action_delete_never_fetched_items,
            "Show Subscription Queue": self.app.action_show_subscription_queue,
        }
        
        for label, action in commands.items():
            score = matcher.match(label)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(label),
                    action,
                    help=f"Action: {label}",
                )

def app_bindings(platform: str | None = None) -> list[tuple[str, str, str]]:
    """The app's key bindings, with the folder key only where it can work.

    Opening a downloaded item's folder is a Windows-only feature, so the plain
    ``o`` binding is built into the list only on Windows. Off Windows the key is
    absent rather than present-and-inert: the front ends must not advertise an
    action that cannot happen. Factored out of the class so the conditional is
    testable on a non-Windows machine.
    """
    bindings = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+d", "show_daemon", "Daemon"),
        ("ctrl+r", "show_stats", "Stats"),
        ("s", "toggle_subscription_queue", "Queue for Subscription"),
        ("i", "ignore_item", "Ignore Item"),
        ("l", "show_subscription_queue", "Subscription Queue"),
        ("ctrl+s", "save_filter_for_scraper", "Save Filter"),
        ("ctrl+w", "toggle_translation", "Toggle Translation"),
        ("ctrl+a", "add_and_row", "AND"),
        ("ctrl+o", "add_or_row", "OR"),
        ("ctrl+x", "delete_bottom_row", "Delete Row"),
        ("ctrl+question_mark", "show_analysis", "Analysis"),
        ("ctrl+b", "subscribe", "Subscribe"),
    ]
    if workshop_folders.is_windows(platform):
        bindings.append(("o", "open_folder", "Open Folder"))
    return bindings


class ScraperApp(App):
    """A Terminal GUI for searching the Steam Workshop database."""

    COMMANDS = {SystemCommandsProvider, DatabaseCommands}

    BINDINGS = app_bindings()

    CSS = """
    #_default {
        layout: vertical;
    }
    #stats-main {
        height: 1fr;
    }
    #stats-scroll {
        /* The metrics column. It scrolls on its own so the tag table beside it
           stays put and keeps the full height. */
        height: 1fr;
        width: 40%;
        padding: 1;
        border-right: tall $primary;
    }
    #stats-right-col {
        height: 1fr;
        width: 60%;
        padding: 1;
    }
    .stats-section {
        height: auto;
        margin-bottom: 1;
    }
    .stats-header {
        color: $accent;
    }
    #tag-stats-scroll {
        /* Fills the column, so the tag list uses the whole screen height and
           scrolls within it rather than being capped at a fixed number of rows. */
        height: 1fr;
    }
    #tag-stats-table, #app-stats-table {
        height: auto;
        border: none;
    }
    #search-container {
        height: auto;
        margin: 0;
        padding: 0 1;
        border: solid $accent;
    }
    #search-builder {
        height: auto;
        max-height: 12;
    }
    .search-row {
        height: 2;
        margin-bottom: 0;
        align: left middle;
    }
    .search-row Select, .search-row Input, .search-row Button {
        height: 1;
        border: none;
        background: $boost;
    }
    .search-row Select > SelectCurrent {
        border: none;
        height: 1;
        padding: 0 1;
        background: $boost;
    }
    .search-buttons {
        height: auto;
        padding-top: 1;
        margin-bottom: 1;
    }
    .search-buttons Button {
        height: 1;
        border: none;
        background: $boost;
        width: auto;
        margin-left: 1;
        color: $accent;
    }
    .search-row Button {
        height: 1;
        border: none;
        background: $boost;
        color: $accent;
    }
    Button.-primary, Button.-error, Button.-success, Button.-warning {
        color: auto 100%;
        background: $primary;
    }
    .search-row Button.-error {
        background: $error;
    }
    .search-row .row-field { width: 20%; }
    .search-row .row-op { width: 20%; }
    .search-row .row-input { width: 35%; }
    .search-row .row-btn { width: 8%; min-width: 0; margin-left: 1; }
    .search-row .row-btn-remove { width: 5%; min-width: 0; margin-left: 1; }

    #main-container {
        layout: horizontal;
    }
    #results-column {
        width: 40%;
        layout: vertical;
    }
    #results-list {
        height: 1fr;
        border: solid $success;
        margin: 0;
    }
    #compact-buttons {
        height: auto;
        margin: 0;
    }
    .compact-btn {
        height: 1;
        border: none;
        min-width: 12;
    }
    #sort-container {
        height: auto;
        layout: horizontal;
        border: solid $primary;
        margin-bottom: 0;
        padding: 0 1;
    }
    #sort-container Select {
        height: 1;
        border: none;
        background: $boost;
        margin-top: 1;
        margin-bottom: 1;
    }
    #sort-container Select > SelectCurrent {
        border: none;
        height: 1;
        padding: 0 1;
        background: $boost;
    }
    .sort-select { width: 1fr; }
    .sort-order { width: 10; }
    /* The Subscribed overlay shares the sort row: a label and its own Select, so
       it sits with the controls it behaves like rather than in the builder. */
    .overlay-label { width: 12; height: 1; margin-top: 1; margin-bottom: 1; }
    .overlay-select { width: 1fr; }

    #detail-container {
        width: 60%;
        border: solid $secondary;
        padding: 0 1;
        margin: 0;
        layout: vertical;
    }
    #detail-pane {
        height: 1fr;
    }
    #details-buttons-row {
        height: 1;
        margin-bottom: 0;
    }
    #top-left-buttons {
        width: 1fr;
    }
    .details-button {
        height: 1;
        border: none;
        padding: 0 1;
        min-width: 0;
        margin-right: 1;
        background: $boost;
        color: $accent;
    }
    #btn-jump-author {
        height: 1;
        border: none;
        padding: 0 1;
        min-width: 0;
        background: $primary;
        color: auto;
        display: none;
    }
    #title-creator-row {
        height: auto;
        margin-top: 0;
    }
    #item-title {
        width: 1fr;
        content-align: left middle;
    }
    #item-creator {
        width: auto;
        max-width: 50%;
        content-align: right middle;
    }
    .blank-line {
        height: 1;
    }
    #stats-row {
        height: auto;
    }
    .stats-col {
        width: 50%;
        height: auto;
    }
    #desc-container {
        border-top: solid $primary;
        border-right: none;
        border-bottom: none;
        border-left: none;
        margin: 0;
        padding: 0;
        height: auto;
    }
    #translation-notice {
        color: $text-muted;
        margin: 0 0 1 0;
        height: auto;
    }
    #subscription-queue-container {
        width: 80%;
        height: 80%;
        background: $surface;
        border: thick $primary;
        padding: 1;
    }
    #subscription-queue-title {
        width: 100%;
        text-align: center;
        text-style: bold;
        padding-bottom: 1;
    }
    """

    def get_system_commands(self, screen):
        """Filter out Screenshot and Theme commands from the system commands."""
        for command in super().get_system_commands(screen):
            if "screenshot" in command.title.lower() or "theme" in command.title.lower():
                continue
            yield command

    def __init__(self, config_path: str = "config.yaml"):
        super().__init__()
        self.theme = "textual-dark"
        self.config_path = config_path
        
        try:
            self.config = load_config(config_path)
        except FileNotFoundError:
            self.config = {"database": {"path": "workshop.db"}}
        except ConfigError as exc:
            print(f"\n{exc}\n", file=sys.stderr)
            raise SystemExit(2)
        self.db_path = self.config["database"]["path"]
        # One controller for the process: the TUI screen and the embedded web
        # server both drive the daemon through it, so a start from either UI is
        # visible to the other. Built before the database is initialised because
        # a pending migration must stop the daemon before it runs.
        self._daemon_controller = DaemonController(self.config_path, config=self.config)
        try:
            initialize_database_with_daemon_stopped(
                self.db_path, self._daemon_controller)
        except (SchemaVersionError, DaemonStillRunningError) as exc:
            # Same handoff as the ConfigError branch above: the sentence goes to
            # stderr and the process exits 2. The screen must not mount against a
            # schema this build does not understand, or migrate while the daemon
            # is still writing to it -- refusing is the whole answer.
            print(f"\n{exc}\n", file=sys.stderr)
            raise SystemExit(2)
        self._wilson_cutoffs = {}
        self._web_port = None
        # The downloaded-star folder helper: one locator for this process, shared
        # with the embedded web server through module-level discovery caching. The
        # detail pane reads `is_supported()` to decide whether to draw its button, and
        # the periodic scan below uses it. One startup line says why it is off.
        self.workshop_folders = workshop_folders.WorkshopFolders(self.db_path, self.config)
        self.workshop_folders.log_status()
        self._start_webserver()
        self.current_item_creator = None
        self.pause_lock_file = ".pauselock"
        # One-shot timer that re-reads rendered rows whose subscription marker
        # is still queued. Armed only while such a row exists, and disarmed by
        # its own tick when none does; see `_start_subscription_poll`.
        self._sub_poll_timer = None
        # The one item-update path: every component displaying an item
        # subscribes here, and every update this process receives is handed to
        # it through `dispatch_item_update`. See `src/item_updates.py` and
        # `docs/tui.md#one-item-update-path`.
        self.item_updates = item_updates.ItemUpdateRegistry()
        
        # Pagination state
        self.current_offset = 0
        self.has_more_results = True
        self.is_loading = False
        self.is_author_mode = False
        # Filters that the last "Jump to Author" replaced, kept in memory so the
        # Return button can put them back without depending on a state file read.
        self._pre_jump_filters: list[dict] | None = None
        # The creator the single-creator view is pinned to, or None outside it.
        # The creator-ignore control acts on this, never on the highlighted row:
        # after the toggle the view may be empty (every item settled), so there
        # is no row left to read a creator from.
        self.author_mode_creator = None
        
        # UI State recovery
        # We use a hidden file to avoid cluttering the working directory
        self.state_file = ".tui_state.yaml"
        self._initial_state = load_tui_state(self.state_file)
        self._restored_scroll_y = self._initial_state.get("scroll_y", 0)
        self._restored_selected_id = self._initial_state.get("selected_workshop_id", None)
        self._initial_load_done = False

    def _handle_exception(self, error: Exception) -> None:
        """Write a crash dump before Textual renders the error and exits.

        This is the only hook that sees the common case: Textual catches an
        unhandled error from its message pump and its workers itself, so neither
        ``sys.excepthook`` nor ``threading.excepthook`` is reached. The dump is
        written first -- a rich console traceback is gone the moment the screen
        is closed -- and the delegate call is unchanged, so Textual still renders
        its own traceback (with locals, which the file also carries redacted) and
        still exits.
        """
        try:
            crash.record_exception(type(error), error, error.__traceback__)
        except Exception:
            logging.error("Crash dump failed", exc_info=True)
        super()._handle_exception(error)

    def save_state(self) -> None:
        """Saves current UI state to disk."""
        if not self.is_mounted or not self._initial_load_done or self.is_author_mode:
            return
            
        try:
            builder = self.query_one("#search-builder", SearchBuilder)
            filters = builder.get_filters()
            sort_by = self.query_one("#sort-by", Select).value
            sort_order = self.query_one("#sort-order", Select).value
            list_view = self.query_one("#results-list", ListView)
            
            selected_id = None
            if list_view.index is not None and list_view.index < len(list_view.children):
                item = list_view.children[list_view.index]
                if hasattr(item, 'item_data'):
                    selected_id = item.item_data.get("workshop_id")
            
            state = {
                "filters": filters,
                "sort_by": sort_by,
                "sort_order": sort_order,
                # The overlay is view state, not a builder row: it travels with
                # sort_by/sort_order so a reload, a builder change and the
                # single-creator jump's restore all keep it. It is written raw,
                # so a value chosen while the control was greyed out (the builder
                # holds a Subscribed row) is not lost; the search ignores it
                # while it is greyed out.
                "subscribed_overlay": self._subscribed_overlay_value(),
                "scroll_y": list_view.scroll_y,
                "selected_workshop_id": selected_id
            }
            save_tui_state(self.state_file, state)
        except Exception as exc:
            pass
            logging.debug("TUI state save skipped: %s", exc)

    # --- the Subscribed overlay control -------------------------------------
    #
    # A labelled Select ("Subscribed:") next to the sort controls. It ANDs one
    # predicate onto every search in addition to the builder's rows, and it is
    # deliberately not a row: changing the builder does not clear it, it never
    # appears in the builder, and "Save Filter for Scraper" does not write it.
    # `any` is its off switch, so it has no separate enable control.

    def _subscribed_overlay_value(self) -> str:
        """The control's own value, kept even while the control is greyed out."""
        try:
            value = self.query_one("#subscribed-overlay", Select).value
        except Exception:
            return "any"
        return value if isinstance(value, str) and value in SUBSCRIBED_VALUES else "any"

    def _builder_has_subscribed_row(self) -> bool:
        """Whether any builder row constrains the Subscribed field."""
        try:
            builder = self.query_one("#search-builder", SearchBuilder)
        except Exception:
            return False
        for row in builder.query(SearchRow):
            try:
                if row.query_one("#field-select", Select).value == SUBSCRIBED_FIELD:
                    return True
            except Exception:
                continue
        return False

    def _effective_subscribed_overlay(self) -> str:
        """What the next search applies: `any` while the builder owns the field.

        A greyed-out overlay is ignored rather than ANDed: the builder row is the
        constraint the user can see, and silently ANDing a hidden second one is
        how a search comes back empty with no visible reason.
        """
        if self._builder_has_subscribed_row():
            return "any"
        return self._subscribed_overlay_value()

    def _sync_subscribed_overlay(self) -> None:
        """Greys the overlay out while the builder has a Subscribed row."""
        try:
            overlay = self.query_one("#subscribed-overlay", Select)
        except Exception:
            return
        blocked = self._builder_has_subscribed_row()
        overlay.disabled = blocked
        overlay.tooltip = SUBSCRIBED_OVERLAY_TOOLTIP if blocked else None

    def on_mount(self) -> None:
        """Initialize the UI and recover state."""
        self.query_one("#btn-return", Button).display = False
        self.query_one("#btn-ignore-creator", Button).display = False
        # Recover sorting and filters
        if self._initial_state:
            try:
                if "sort_by" in self._initial_state:
                    self.query_one("#sort-by", Select).value = self._initial_state["sort_by"]
                if "sort_order" in self._initial_state:
                    self.query_one("#sort-order", Select).value = self._initial_state["sort_order"]
                overlay_value = normalise_subscribed_value(
                    self._initial_state.get("subscribed_overlay"))
                if overlay_value in SUBSCRIBED_VALUES:
                    self.query_one("#subscribed-overlay", Select).value = overlay_value
                if "filters" in self._initial_state:
                    builder = self.query_one("#search-builder", SearchBuilder)
                    builder.set_filters(self._initial_state["filters"])
            except Exception:
                logging.debug("Failed to restore filter state from initial load")
                pass

        # The builder's rows mount asynchronously, so the grey-out rule is
        # applied once they are composed rather than immediately here.
        self.call_after_refresh(self._sync_subscribed_overlay)
        self.call_after_refresh(self.execute_search)
        
        # Watch the scroll_y property to trigger infinite loading
        list_view = self.query_one("#results-list", ListView)
        self.watch(list_view, "scroll_y", self._check_scroll_bottom)

        # Animate braille spinner on pending items
        self.set_interval(0.15, self._tick_spinners)

        # The general item-update poll: the trigger that is not conditional on
        # anything being pending. It runs for as long as this screen is open and
        # reads only the ids the registry holds, so a change written behind this
        # process's back reaches every display of the item -- see
        # `_poll_item_updates`.
        self.set_interval(item_updates.ITEM_UPDATE_POLL_SECONDS, self._poll_item_updates)

        # The downloaded-star scan, on the daemon's own cadence. It is skipped
        # while this process can see a daemon running, because the daemon runs
        # the same scan and two of them would check the same folders in
        # parallel. Off Windows the scan is a no-op and reads nothing.
        self.set_interval(workshop_folders.DOWNLOADED_ITEM_SCAN_INTERVAL_SECONDS,
                          self._maybe_scan_downloaded_items)

    def _check_scroll_bottom(self, scroll_y: float) -> None:
        self.save_state()
        try:
            list_view = self.query_one("#results-list", ListView)
            if list_view.max_scroll_y == 0:
                return
            if scroll_y >= list_view.max_scroll_y - 5:
                self.run_worker(self.load_more_items())
        except Exception:
            logging.debug("Scroll-triggered load-more check failed")
            pass

    async def _tick_spinners(self) -> None:
        WorkshopItem._tick = (WorkshopItem._tick + 1) % WorkshopItem._TICK_MODULUS
        try:
            list_view = self.query_one("#results-list", ListView)
            for child in list_view.children:
                if hasattr(child, 'item_data') and pending.pending_stage(child.item_data):
                    await child.refresh_item()
        # Cosmetic spinner refresh; a failure is retried on the next 0.15 s tick.
        except Exception:
            pass

    # --- the one item-update path -------------------------------------------

    def dispatch_item_update(self, item: dict) -> int:
        """Hand one item's block to every component displaying that id.

        This is the TUI's single dispatch point. A callback, a poll, an action
        and the daemon's folder scan all end here, and none of them knows which
        component draws the item: the registry holds whatever subscribed to the
        ``workshop_id`` while it is on screen. See `src/item_updates.py`.
        """
        return self.item_updates.dispatch(item)

    def dispatch_item_updates(self, items) -> int:
        """Dispatch a batch of blocks; returns the number of deliveries."""
        return self.item_updates.dispatch_many(items)

    @db_poll.guard_db_poll("item update poll")
    def _poll_item_updates(self) -> None:
        """The general trigger: refresh everything on screen, pending or not.

        One batched read of exactly the ids the registry holds -- the rendered
        rows plus the detail pane, never a table scan and never one read per
        component -- and one dispatch of the blocks to their subscribers. It is
        deliberately not conditional on a spinner or a queued marker: that
        condition is what let a change written behind this process's back (the
        daemon's folder scan stamping ``steam_download_seen_at``) sit unfetched,
        leaving the row and the pane disagreeing about the same item.

        An empty registry does no read at all, so a screen with nothing on it
        costs nothing. A transient lock skips this tick; the next one is the
        retry, and the interval is the term the guard's docstring promises.
        """
        workshop_ids = self.item_updates.workshop_ids()
        if not workshop_ids:
            return
        self.dispatch_item_updates(get_items_by_ids(self.db_path, workshop_ids))

    # --- the subscription-marker poll ---------------------------------------

    def _queued_subscription_ids(self) -> list[int]:
        """Ids of rendered rows whose subscription marker is still ``pending``.

        The selection is made from what is *rendered* -- the item data the row
        was built from, resolved through the shared table -- rather than from a
        separately read queue flag, so every row still drawing the green outline
        is re-read and every settled row is left alone.
        """
        try:
            list_view = self.query_one("#results-list", ListView)
        except Exception:
            return []
        ids: list[int] = []
        for child in list_view.children:
            data = getattr(child, "item_data", None)
            if not data:
                continue
            if subscription.subscription_state(data) not in (
                    subscription.QUEUED, subscription.QUEUED_REMOVE):
                continue
            workshop_id = data.get("workshop_id")
            if workshop_id is not None:
                ids.append(workshop_id)
        return ids

    def _start_subscription_poll(self, delay: float = 0.05) -> None:
        """Arm a one-shot re-read of the rendered rows that are still queued.

        The web grid's ``_startListPoll`` is the model: a new search, a row
        moving into ``queued``, or a pass result all arm the next tick, and the
        tick re-arms itself only while a rendered row is still queued. A
        settled list therefore costs no reads, and there is no fixed interval.
        The default is a hair above zero rather than Textual's ``set_timer(0)``,
        whose zero interval divides by zero when a busy loop skips it.
        """
        self._stop_subscription_poll()
        self._sub_poll_timer = self.set_timer(delay, self._poll_queued_subscriptions)

    def _stop_subscription_poll(self) -> None:
        if self._sub_poll_timer is not None:
            self._sub_poll_timer.stop()
            self._sub_poll_timer = None

    async def _poll_queued_subscriptions(self) -> None:
        """Re-read the rendered queued rows once; re-arm only if some remain."""
        self._sub_poll_timer = None
        if not self.is_mounted:
            return
        ids = self._queued_subscription_ids()
        if not ids:
            self._stop_subscription_poll()
            return
        await self.refresh_subscription_rows(ids)
        remaining = len(self._queued_subscription_ids())
        if not remaining:
            self._stop_subscription_poll()
            return
        # The web poll's adaptive delay: faster while more rows are outstanding.
        self._start_subscription_poll(max(1.0, math.log2(remaining)))

    @db_poll.guard_db_poll("subscription marker poll")
    async def refresh_subscription_rows(self, workshop_ids) -> None:
        """Read the given ids' marker columns and dispatch them as a block.

        This is the fast path, not the mechanism: the general
        :meth:`_poll_item_updates` is what guarantees that every displayed item
        is refreshed whether or not anything is pending. This one exists so a
        row whose marker moved during a subscribe pass moves at once rather than
        at the general poll's next tick. It used to redraw the row in place and
        only for the four subscription columns; it now dispatches the block
        through the same one path as every other update, and a subscriber merges
        it, so the block's narrowness costs the row nothing.

        The reader is the shared database, so a write by *any* process -- this
        TUI's own subscribe pass, the web UI's routes, or the daemon's daily
        reconcile -- is picked up; no writer is hooked and no callback is
        required.

        A transient lock skips this read without raising. That matters twice
        over: the one-shot poll re-arms itself from the rendered state, so the
        next tick retries, and the subscribe result's fast path reaches this
        method from a callback of its own, where an exception would take the
        session down just as the timer did.
        """
        states = get_subscription_states(self.db_path, set(workshop_ids))
        self.dispatch_item_updates(states.values())

    @db_poll.guard_db_poll("downloaded-item scan")
    def _maybe_scan_downloaded_items(self) -> None:
        """Stamp subscribed items Steam has downloaded, unless a daemon will.

        The daemon runs the same scan, so this TUI's copy is skipped while the
        controller can see a daemon: two scans would stat the same folders in
        parallel for one answer. The interval is the daemon's
        (``DOWNLOADED_ITEM_SCAN_INTERVAL_SECONDS``), because it is the same work.

        The read/write is guarded like every unattended database callback: a
        transient lock skips this tick, and the next one is the retry. Off
        Windows the scan returns without touching the database.
        """
        if self._daemon_controller.is_running():
            return
        self.workshop_folders.scan_downloads()

    def compose(self) -> ComposeResult:
        yield Header()
        
        search_builder = SearchBuilder(id="search-builder")
        search_container = Vertical(
            search_builder,
            Horizontal(
                # The creator-scoped ignore toggle. It is shown only in
                # single-creator mode, and it sits at the far end of the row from
                # Return (Search between them) so a press meant for Return cannot
                # land on it. It acts on the creator being viewed, not on the
                # highlighted item; its label names the next move.
                Button("Ignore creator", id="btn-ignore-creator", variant="error"),
                Button("Search", id="btn-search", variant="primary"),
                Button("Save Filter for Scraper", id="btn-save-filter", variant="default"),
                Button("Return", id="btn-return", variant="warning"),
                classes="search-buttons"
            ),
            id="search-container"
        )
        search_container.border_title = "Filters"
        if self._web_port:
            search_container.border_subtitle = f"web :{self._web_port}"

        sort_options = [
            ("Title", "title"),
            ("File Size", "file_size"),
            ("Subscriptions", "subscriptions"),
            ("Favorited", "favorited"),
            ("Views", "views"),
            ("Workshop ID", "workshop_id"),
            ("Created Time", "steam_created_at"),
            ("Updated Time", "steam_updated_at"),
            ("Fetched Time", "api_fetched_at"),
            ("Subscriber Score", "wilson_subscription_score"),
            ("Favorite Score", "wilson_favorite_score"),
            ("Subscribed at", "own_first_subscribed_at"),
        ]
        
        # The Subscribed overlay sits with the sort controls, as it does in the
        # web UI. Its value is a view-level constraint ANDed onto whatever the
        # builder says; `any` means no constraint.
        sort_container = Horizontal(
            Label("Subscribed:", classes="overlay-label"),
            Select([(v, v) for v in SUBSCRIBED_VALUES], value="any",
                   id="subscribed-overlay", classes="overlay-select"),
            Select(sort_options, value="title", id="sort-by", classes="sort-select"),
            Select([("ASC", "ASC"), ("DESC", "DESC")], value="ASC", id="sort-order", classes="sort-order"),
            id="sort-container"
        )
        sort_container.border_title = "Sort"

        results_list = ListView(id="results-list")
        results_list.border_title = "Items"

        compact_buttons = Horizontal(
            Button("Fetch New", id="btn-fetch-new", classes="compact-btn"),
            Button("Update Visible", id="btn-update-visible", classes="compact-btn"),
            id="compact-buttons"
        )

        details_container = Vertical(
            DetailsPane(id="detail-pane"),
            id="detail-container"
        )
        details_container.border_title = "Details"

        yield search_container
        yield Horizontal(
            Vertical(
                sort_container,
                results_list,
                compact_buttons,
                id="results-column"
            ),
            details_container,
            id="main-container"
        )
        yield Footer()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        pass  # search only on explicit "Search" click

    async def on_select_changed(self, event: Select.Changed) -> None:
        # A field change may have added or removed the builder's Subscribed row,
        # which decides whether the overlay is usable.
        self._sync_subscribed_overlay()
        # Save state when sort/filter changes, but don't auto-search
        if event.value is not None and self._initial_load_done:
            self.save_state()

    async def execute_search(self) -> None:
        """Executes a new search, resetting pagination."""
        if not self.is_mounted:
            return
            
        self.current_offset = 0
        self.has_more_results = True
        
        try:
            list_view = self.query_one("#results-list", ListView)
        except Exception:
            logging.debug("Results list not accessible during search execution")
            return
        await list_view.clear()
        # A fresh result set has no rendered rows to watch until they mount.
        self._stop_subscription_poll()
        self._compute_percentiles()
        await self.load_more_items()

    def _compute_percentiles(self) -> None:
        """Computes Wilson score percentile cutoffs for the current filter set."""
        try:
            builder = self.query_one("#search-builder", SearchBuilder)
            filters = builder.get_filters()
        except Exception:
            logging.debug("Search builder not accessible during percentile computation")
            return
        self._wilson_cutoffs = compute_wilson_cutoffs(
            self.db_path, filters,
            subscribed_overlay=self._effective_subscribed_overlay())

    async def load_more_items(self) -> None:
        """Fetches the next chunk of items from the database."""
        if self.is_loading or not self.has_more_results:
            return
            
        self.is_loading = True
        
        search_builder = self.query_one("#search-builder", SearchBuilder)
        filters = search_builder.get_filters()
        
        sort_by = self.query_one("#sort-by", Select).value
        sort_order = self.query_one("#sort-order", Select).value

        # Handle potential Select.BLANK
        if not isinstance(sort_by, str): sort_by = "title"
        if not isinstance(sort_order, str): sort_order = "ASC"

        results = search_items(
            self.db_path, 
            filters=filters,
            sort_by=sort_by,
            sort_order=sort_order,
            summary_only=True,
            limit=50,
            offset=self.current_offset,
            subscribed_overlay=self._effective_subscribed_overlay()
        )
        
        list_view = self.query_one("#results-list", ListView)
        
        items = [WorkshopItem(item) for item in results]
        await list_view.mount(*items)

        # Watch the rows that arrived already queued for subscription; the poll
        # disarms itself at once when none of them is queued.
        self._start_subscription_poll()

        for item in results:
            if item.get("web_scrape_priority", 0) > 0:
                raise_web_scrape_priority_for_list(self.db_path, item["workshop_id"])
                raise_translation_priority_for_list(self.db_path, item["workshop_id"])
            if item.get("image_priority", 0) > 0:
                raise_image_priority_for_list(self.db_path, item["workshop_id"])
            raise_api_priority_for_list(self.db_path, item["workshop_id"])
            
        self.current_offset += len(results)
        
        if len(results) < 50:
            self.has_more_results = False
            
        self.is_loading = False

        if not self._initial_load_done:
            self._initial_load_done = True
            
            def restore_state():
                try:
                    if self._restored_selected_id:
                        for i, item in enumerate(list_view.children):
                            if getattr(item, 'item_data', {}).get("workshop_id") == self._restored_selected_id:
                                list_view.index = i
                                break
                                
                    if self._restored_scroll_y > 0:
                        list_view.scroll_y = self._restored_scroll_y
                except Exception:
                    logging.debug("Failed to restore scroll position")
                    pass

            self.call_after_refresh(restore_state)
        else:
            self.save_state()

    async def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Load more items when scrolling near the bottom of the list, and update details pane."""
        list_view = event.list_view
        if list_view.id == "results-list":
            # If we are within 10 items of the end, fetch more
            if list_view.index is not None and list_view.index >= len(list_view) - 10:
                await self.load_more_items()
                
            if event.item and hasattr(event.item, 'item_data'):
                item_data = event.item.item_data
                self.current_item_creator = item_data.get('creator_steamid')
                # Detail priority is applied by DetailsPane when it adopts the
                # item, not here: this handler fires on every highlight move.
                detail_pane = self.query_one("#detail-pane", DetailsPane)
                detail_pane.workshop_id = item_data.get("workshop_id")
                
                jump_btn = self.query_one("#btn-jump-author", Button)
                if self.current_item_creator:
                    jump_btn.display = True
                else:
                    jump_btn.display = False
                    
                self.save_state()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle selection of an item in the list."""
        # Selection logic moved to highlighted event for immediate viewing
        self.save_state()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses (e.g., Jump to Author, Translation, Search Builder buttons)."""
        if event.button.id == "btn-fetch-new":
            with open('.fetch_new', 'w') as handle:
                handle.write('1')
            self.notify("Fetch-new triggered! The daemon will scan recently-updated items on its next cycle.")
        elif event.button.id == "btn-update-visible":
            await self.action_update_visible()
        elif event.button.id == "btn-search":
            await self.execute_search()
        elif event.button.id == "btn-save-filter":
            await self.action_save_filter_for_scraper()
        elif event.button.id in ("btn-and", "btn-or"):
            logic = "AND" if event.button.id == "btn-and" else "OR"
            self.query_one("#search-builder", SearchBuilder).add_row(logic)
            self.call_after_refresh(self._sync_subscribed_overlay)
        
        elif event.button.id == "btn-remove":
            row = event.button.parent
            if isinstance(row, SearchRow):
                await row.remove()
            self._sync_subscribed_overlay()

        elif event.button.id == "btn-return":
            self.action_return_from_author_mode()

        elif event.button.id == "btn-ignore-creator":
            await self.action_toggle_creator_ignored()

        elif event.button.id == "btn-jump-author" and self.current_item_creator:
            # Save state before switching to single creator mode
            if not self.is_author_mode:
                self.save_state()
                # Snapshot what the jump is about to throw away so Return can
                # restore it. This is in memory on purpose: the on-disk snapshot
                # above is for a restart, and a later state write could overwrite
                # it before the user presses Return.
                self._pre_jump_filters = self.query_one(
                    "#search-builder", SearchBuilder
                ).get_filters()

            self.is_author_mode = True
            self.author_mode_creator = self.current_item_creator
            self.query_one("#btn-save-filter", Button).display = False
            self.query_one("#btn-return", Button).display = True
            # Label the creator toggle from the stored flag. This is the view's
            # own answer to "which creator is being viewed": the highlighted
            # row can change (or vanish, once the toggle settles every item), so
            # the action reads this attribute rather than `current_item_creator`.
            ignore_btn = self.query_one("#btn-ignore-creator", Button)
            ignore_btn.display = True
            ignore_btn.label = creator_ignore_label(
                creator_is_ignored(self.db_path, self.author_mode_creator))

            builder = self.query_one("#search-builder", SearchBuilder)

            # Clear all current rows
            await builder.query(SearchRow).remove()

            # Build the row already carrying the author filter. Assigning the
            # Selects after mount used to race the field's Change handler, which
            # is what populates the op list: "is" was rejected for the default
            # text field before that handler ran.
            new_row = SearchRow(
                builder.fields,
                builder.field_ops_map,
                is_first=True,
                initial_filter={
                    "field": "Author ID",
                    "op": "is",
                    "value": str(self.current_item_creator),
                },
            )
            await builder.mount(new_row)

            # Use call_after_refresh to ensure selects are populated
            def setup_author_filter():
                # The overlay is a view control, not a builder row, so the jump
                # leaves its value alone; only its enabled state can change,
                # because the author row is not a Subscribed row.
                self._sync_subscribed_overlay()
                self.run_worker(self.execute_search())

            self.call_after_refresh(setup_author_filter)

        elif event.button.id == "btn-open-folder":
            # Acts on the pane's item, the same one the marker describes; the
            # `o` key acts on the highlighted list item.
            detail = self.query_one("#detail-pane", DetailsPane)
            self.open_folder_for(detail.workshop_id)

        elif event.button.id == "btn-toggle-translation":
            self.action_toggle_translation()

    def action_return_from_author_mode(self) -> None:
        """Leaves single-creator mode and puts back the filters the jump replaced.

        The filter snapshot is the one the jump took in memory. Restoring it is
        deferred to the next refresh because ``set_filters`` mounts rows
        asynchronously and the rows must be composed before they can be read
        back or saved. Clearing the flag first makes ``save_state`` accept writes
        again immediately, so returning always leaves the app usable even if
        there is no snapshot to restore.
        """
        filters = self._pre_jump_filters
        self._pre_jump_filters = None

        self.is_author_mode = False
        self.author_mode_creator = None
        self.query_one("#btn-save-filter", Button).display = True
        self.query_one("#btn-return", Button).display = False
        self.query_one("#btn-ignore-creator", Button).display = False

        if filters is None:
            self.save_state()
            return

        builder = self.query_one("#search-builder", SearchBuilder)
        builder.set_filters(filters)

        def after_restore() -> None:
            self._sync_subscribed_overlay()
            self.save_state()
            self.run_worker(self.execute_search())

        self.call_after_refresh(after_restore)

    async def action_toggle_creator_ignored(self) -> None:
        """Toggle the owner's ignore flag on the creator the view is pinned to.

        The creator-scoped view is the ``btn-jump-author`` filtered result list,
        and it knows its creator from :attr:`author_mode_creator`, which the jump
        sets from the highlighted row and Return clears. That attribute is used
        rather than ``current_item_creator`` because the highlighted row is not
        stable here: after an ignore settles every one of the creator's items the
        re-query below leaves the list empty, so there may be no row left to read
        a creator from -- while the button must still be pressable to reverse it.

        ``toggle_creator_ignored`` owns the direction and ``creator_ignore_label``
        owns the wording, both in ``src/database``, so this button and the web
        route cannot disagree about what a second press does or says. The write
        is a whole-view change -- every item of the creator settles or comes back
        -- so the results are re-queried rather than patched in place, unlike the
        single-row ``i`` toggle.
        """
        if not self.is_author_mode or not self.author_mode_creator:
            return
        ignored = toggle_creator_ignored(self.db_path, self.author_mode_creator)
        button = self.query_one("#btn-ignore-creator", Button)
        button.label = creator_ignore_label(ignored)
        verb = "Ignored" if ignored else "Un-ignored"
        self.notify(f"{verb} creator {self.author_mode_creator}.")
        await self.execute_search()

    async def action_save_filter_for_scraper(self) -> None:
        builder = self.query_one("#search-builder", SearchBuilder)
        filters = builder.get_filters()
        current_appid = self.config.get("daemon", {}).get("target_appids", [None])[0]

        if current_appid is None:
            self.notify("No target AppID configured for saving filter.", severity="error")
            return

        save_enrichment_filters(self.db_path, current_appid, enrichment_filters=json.dumps(filters))
        self.notify(f"Filter saved for AppID {current_appid}. Scraper will use this for enrichment.")

    def action_toggle_translation(self) -> None:
        detail_pane = self.query_one("#detail-pane", DetailsPane)
        detail_pane.show_translated = not detail_pane.show_translated
        
    async def action_toggle_subscription_queue(self) -> None:
        """Toggles the subscription queue status of the highlighted item."""
        list_view = self.query_one("#results-list", ListView)
        if list_view.index is None:
            return

        item = list_view.highlighted_child
        if not item or not hasattr(item, "item_data"):
            return

        workshop_id = item.item_data.get("workshop_id")
        if not workshop_id:
            return

        # Toggle in DB
        toggle_subscription_queue(self.db_path, workshop_id)

        # The change reaches the row and the pane through the one dispatch
        # point. Patching the row here and the pane below is what the registry
        # replaces: any component showing this id hears the update, including
        # one added later, and neither has to be named by this action.
        states = get_subscription_states(self.db_path, [workshop_id])
        fresh = states.get(workshop_id)
        if fresh:
            self.dispatch_item_update(fresh)

        # A row queued from the keyboard is watched too: the web grid starts its
        # poll on the transition into `queued` for the same reason, and the
        # removal direction (`queued_remove`) lands the same way.
        if fresh and subscription.subscription_state(fresh) in (
                subscription.QUEUED, subscription.QUEUED_REMOVE):
            self._start_subscription_poll()

        # Move to next item
        if list_view.index < len(list_view) - 1:
            list_view.index += 1
        
        # Scroll to keep highlight visible if needed
        # list_view.scroll_to_widget(item)

    async def action_ignore_item(self) -> None:
        """Toggle the owner's ignored marker on the highlighted item.

        Modelled on :meth:`action_toggle_subscription_queue`: the same key
        restores an ignored item, the change reaches the row through the one
        dispatch point (which redraws its title underline), and the selection
        then moves to the next row. The row is deliberately **not** dropped --
        the list is only re-queried by the next search, and the live session has
        to show the marker the key just set, the same way the web grid keeps the
        cell until its next query.
        """
        list_view = self.query_one("#results-list", ListView)
        if list_view.index is None:
            return

        item = list_view.highlighted_child
        if not item or not hasattr(item, "item_data"):
            return

        workshop_id = item.item_data.get("workshop_id")
        if not workshop_id:
            return

        # The direction lives in the shared toggle, so this key and the web
        # route cannot disagree about what a second press does.
        toggle_ignored_item(self.db_path, workshop_id)

        # The change reaches the row (and the pane, if it holds this item)
        # through the one dispatch point. The full row read carries
        # `fetch_status`, which is what the row's title underlines from.
        fresh = get_items_by_ids(self.db_path, [workshop_id])
        if fresh:
            self.dispatch_item_update(fresh[0])

        # Move to the next item; the ignored row stays where it is.
        if list_view.index < len(list_view) - 1:
            list_view.index += 1

    async def action_open_folder(self) -> None:
        """Open the highlighted item's folder, like ``s`` acts on the highlighted item.

        Windows-only: the binding that reaches here is only built on Windows, so
        this is never invoked elsewhere. When the item is not green the same
        refusal the disabled button carries is shown as a notification, rather
        than the key silently doing nothing.
        """
        list_view = self.query_one("#results-list", ListView)
        item = list_view.highlighted_child
        if not item or not hasattr(item, "item_data"):
            self.notify("No item highlighted to open.", severity="warning")
            return
        self.open_folder_for(item.item_data.get("workshop_id"))

    def open_folder_for(self, workshop_id) -> None:
        """Run the shared open action and report its result.

        The shared helper owns every guard -- Windows only, the item must be in
        the ``downloaded`` state, the folder must still be on disk -- and changes
        no state when it refuses. This only turns the result into a notification:
        an info line for a folder that opened, a warning for a refusal.
        """
        if not workshop_id:
            self.notify("No item selected to open.", severity="warning")
            return
        result = self.workshop_folders.open_folder(workshop_id)
        severity = "information" if result["ok"] else "warning"
        self.notify(result["message"], severity=severity)

    async def action_add_and_row(self) -> None:
        self.query_one("#search-builder", SearchBuilder).add_row("AND")
        await self.execute_search()

    async def action_add_or_row(self) -> None:
        self.query_one("#search-builder", SearchBuilder).add_row("OR")
        await self.execute_search()

    async def action_delete_bottom_row(self) -> None:
        builder = self.query_one("#search-builder", SearchBuilder)
        rows = list(builder.query(SearchRow))
        if len(rows) > 1:
            row_to_delete = rows[-1]
            row_to_delete.remove()
            self.call_after_refresh(self.execute_search)

    def action_delete_never_fetched_items(self) -> None:
        """Deletes items that were never successfully fetched."""
        count = delete_never_fetched_items(self.db_path)
        self.notify(f"Deleted {count} never-fetched item(s).")
        self.run_worker(self.execute_search())

    def action_show_stats(self) -> None:
        """Shows the database statistics screen."""
        daemon_config = self.config.get("daemon", {}) or {}
        self.push_screen(StatsScreen(
            self.db_path,
            daemon_config.get("target_appids"),
            staleness_days=metrics.item_staleness_days(daemon_config),
        ))
        
    async def action_update_visible(self) -> None:
        """Queues all visible list items for API re-fetch (priority 10)."""
        try:
            list_view = self.query_one("#results-list", ListView)
        except Exception:
            return

        # Compute viewport-visible items from scroll position
        scroll_y = list_view.scroll_y
        visible_height = list_view.size.height
        child_height = 2  # each WorkshopItem is 2 lines
        visible_ids = []
        for i, child in enumerate(list_view.children):
            if hasattr(child, 'item_data') and child.item_data:
                top = i * child_height
                bottom = top + child_height
                if bottom > scroll_y and top < scroll_y + visible_height:
                    visible_ids.append(child.item_data["workshop_id"])
                    if hasattr(child, 'refresh_item'):
                        # refresh_item is async (every other call site awaits it);
                        # calling it bare only raised a RuntimeWarning and never
                        # re-composed the row.
                        await child.refresh_item()

        if not visible_ids:
            self.notify("No items visible.")
            return
        conn = get_connection(self.db_path)
        placeholders = ",".join("?" * len(visible_ids))
        # Same guard as the web route: a settled item -- dead (-1) or ignored (-2)
        # -- must not be put back in the API fetch queue by a bulk update.
        conn.execute(
            f"UPDATE workshop_items SET api_priority = 10 WHERE workshop_id IN ({placeholders}) "
            f"AND {live_fetch_status_predicate()}",
            visible_ids,
        )
        conn.commit()
        conn.close()
        self.notify(f"Queued {len(visible_ids)} items for update.")

    def action_show_subscription_queue(self) -> None:
        """Shows the subscription queue modal screen.

        The engine needs the cookie source, so the app's config travels with the
        screen; the screen itself owns the pause for as long as it is open.
        """
        self.push_screen(SubscriptionQueueScreen(
            self.db_path, self.pause_lock_file, self.config))

    async def action_quit(self) -> None:
        """Quit the application."""
        self.exit()

    def action_show_analysis(self) -> None:
        """Shows the view window analysis screen."""
        self.push_screen(AnalysisScreen(self.db_path))

    def action_show_daemon(self) -> None:
        """Shows the daemon management screen."""
        self.push_screen(DaemonManagerScreen(self._daemon_controller))

    async def action_subscribe(self) -> None:
        """Subscribes to the currently displayed workshop item on Steam."""
        try:
            detail = self.query_one("#detail-pane", DetailsPane)
            wid = getattr(detail, "workshop_id", None)
        except Exception:
            logging.warning("Failed to get workshop_id from detail pane for subscribe")
            wid = None
        if not wid:
            self.notify("No item selected to subscribe to.", severity="warning")
            return

        conn = get_connection(self.db_path)
        row = conn.execute(
            "SELECT consumer_appid FROM workshop_items WHERE workshop_id=?",
            (wid,)
        ).fetchone()
        conn.close()

        if not row or not row["consumer_appid"]:
            self.notify("Item has no AppID — cannot subscribe.", severity="warning")
            return

        import requests
        web_port = self._web_port or self.config.get("web", {}).get("port", 8080)
        try:
            resp = requests.post(
                f"http://127.0.0.1:{web_port}/api/subscribe/{wid}",
                timeout=15,
            )
            data = resp.json()
            if data.get("success") == 1:
                self.notify("Subscribed!")
            elif data.get("success") == 2:
                self.notify("Steam session expired. Visit steamcommunity.com to refresh.", severity="warning")
            elif data.get("success") == 15:
                self.notify("Permission denied. Steam session may have expired.", severity="warning")
            elif data.get("success") == 25:
                self.notify("Subscription limit reached (15,000).", severity="warning")
            else:
                self.notify(escape_markup(data.get("message", "Subscribe failed.")), severity="error")
        except Exception as e:
            self.notify(f"Subscribe request failed: {escape_markup(e)}", severity="error")

    def _start_webserver(self) -> None:
        """Starts the embedded web server in a background thread.

        Waitress binds the listening socket inside ``create_server`` and reports
        the port it actually got, so the port the TUI records is the port the
        server serves on: there is no probe-then-close gap for another process to
        grab. A busy configured port still falls back to an ephemeral one, and a
        port chosen here is still persisted to config.
        """
        server = None
        try:
            from src.webserver import app, init_webserver
            from waitress import create_server

            configured = self.config.get("web", {}).get("port")
            port_changed = not configured
            try:
                server = create_server(app, host='0.0.0.0', port=configured or 0)
            except OSError:
                if not configured:
                    raise
                logging.warning(f"Configured port {configured} in use, using a random port")
                server = create_server(app, host='0.0.0.0', port=0)
                port_changed = True

            # ``effective_port`` is the port the bound socket reports, as a
            # string; the rest of the app stores and formats it as an int.
            self._web_port = int(server.effective_port)

            if port_changed:
                self.config.setdefault("web", {})["port"] = self._web_port
                save_config(self.config_path, self.config)
                logging.info(f"Saved port {self._web_port} to config")

            init_webserver(self.db_path, self.config, config_path=self.config_path,
                           daemon_controller=self._daemon_controller)

            def run_server():
                server.run()

            self._web_thread = threading.Thread(target=run_server, daemon=True)
            self._web_thread.start()
            logging.info(f"Web server started on port {self._web_port}")
        except Exception as e:
            # Release the bound socket if anything after the bind failed, so a
            # failed startup does not leave the chosen port occupied.
            if server is not None:
                server.close()
            logging.warning(f"Web server failed to start: {e}")

def main():
    # Before anything can fail: a crash raised while `load_config` runs or while
    # logging is being configured has no destination otherwise -- the log
    # handlers are the thing being built, and the hooks are not installed yet.
    # The hooks do not need logging; `crash.install` below adds the ring buffer
    # once `basicConfig` has run, so a forced configuration cannot drop it.
    crash.install_hooks("tui")
    config_path = "config.yaml"
    import sys
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
        
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        config = {}
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        raise SystemExit(2)
        
    log_config = config.get("logging", {})
    level_str = log_config.get("level", "INFO").upper()
    log_level = getattr(logging, level_str, logging.INFO)
    log_file = log_config.get("file")
    
    # For TUI, prefer file logging only so it doesn't mess up the screen
    handlers = []
    if log_file:
        # UTF-8 explicitly, not the platform default: on Windows the default is
        # cp1252, which corrupts every non-ASCII character the moment the file is
        # read back as UTF-8 and silently drops any record it cannot represent
        # (CJK titles, which this project is full of). See `_log_file_handler` in
        # `src/daemon_runner.py`. The same handler also reopens the file when the
        # operator rotates it -- the TUI is one of the two processes holding the
        # daemon log open, so a rename alone would leave this writer on the
        # renamed inode. See `src/log_rotation.py`.
        handlers.append(log_rotation.log_file_handler(log_file))
        
    if handlers:
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=handlers,
            force=True
        )
    else:
        # Disable logging if no file, as stdout corrupts TUI
        logging.getLogger().addHandler(logging.NullHandler())

    # After logging is configured, so the ring-buffer handler is not dropped by
    # the forced basicConfig above. From here an unhandled error anywhere in the
    # process is written to the outbox; `ScraperApp._handle_exception` covers the
    # errors Textual catches before they ever reach a hook.
    crash.install("tui", config, config_path=config_path)

    app = ScraperApp(config_path)
    try:
        app.run()
    except BaseException:
        # Belt to the hook's braces: a traceback that escapes `run()` still gets
        # a dump before it propagates, and the re-raise keeps the exit the same.
        crash.record_exception(*sys.exc_info())
        raise

if __name__ == "__main__":
    main()
