import json
import logging
import time
from functools import partial
from textual.app import App, ComposeResult, SystemCommand
from textual import on, events
from textual.command import Provider, Hit, DiscoveryHit
from textual.system_commands import SystemCommandsProvider
from typing import Iterable
from textual.screen import Screen, ModalScreen
from textual.widgets import Header, Footer, Input, ListView, ListItem, Static, Label, Select, Button, Markdown, DataTable, RichLog
from textual.containers import Horizontal, Vertical, VerticalScroll, Center, Grid
from textual.reactive import reactive
from textual.worker import Worker, WorkerState
from src.database import search_items, get_all_authors, initialize_database, get_item_details, save_app_filter, clear_pending_items, toggle_subscription_queue_status, get_queued_items, compute_wilson_cutoffs, bump_web_priority_for_list, bump_web_priority_for_detail, bump_translation_for_list, bump_translation_for_detail, bump_image_priority_for_list, bump_image_priority_for_detail, get_connection, FILTER_SCHEMA, ALL_FILTER_FIELDS, bump_api_priority_for_list, bump_api_priority_for_detail
from src.analysis import view_window_analysis
from src import metrics
from src import images
from src import pending
from src.config import ConfigError, load_config, save_config
from src.daemon_control import DaemonController
import os
import yaml
import threading
import webbrowser
import datetime

def format_ts(ts):
    """Converts a Unix timestamp to YYYY-MM-DD string."""
    if not ts: return "N/A"
    try:
        return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
    except Exception:
        logging.debug("format_count failed for value %r", n)
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
        logging.debug("format_size failed for value %r", bytes)
        return "N/A"

def format_count(n):
    """Humanizes a number to 3 significant digits with K/M suffix and color markup."""
    if not n or n == 0:
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
    v = n / 1_000_000
    if n < 10_000_000:
        return f"[yellow]{v:.2f}M[/yellow]"
    elif n < 100_000_000:
        return f"[yellow]{v:.1f}M[/yellow]"
    else:
        return f"[yellow]{v:.0f}M[/yellow]"

def parse_tags(tags) -> list[str]:
    """Parses a comma-separated tag string (junction-table format) or legacy
    JSON into a list of tag-name strings."""
    if not tags:
        return []
    # Junction-table format: comma-separated via GROUP_CONCAT(t.tag_name, ', ')
    if isinstance(tags, str) and not tags.startswith('['):
        return [t.strip() for t in tags.split(',') if t.strip()]
    # Legacy JSON format
    try:
        parsed = json.loads(tags) if isinstance(tags, str) else tags
        return [str(t.get("tag") if isinstance(t, dict) else t) for t in (parsed if isinstance(parsed, list) else [])]
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
    LABELS = {
        "high_water": "Last successful API fetch",
        "totals": "Totals",
        "app_tracking": "App tracking",
        "status_counts": "Status counts",
        "stuck_work": "Stuck work",
        "fetch_recency": "Fetch recency",
        "coverage": "Coverage",
        "translation_status": "Translation status",
        "tag_counts": "Tags",
        "priority_breakdowns": "Queue priorities",
    }

    #: Text metrics own a Static widget; the two table metrics are special-cased
    #: in `_compose_chunk` and `_render_metric`.
    CONTENT_IDS = {
        "high_water": "high-water-content",
        "totals": "totals-content",
        "status_counts": "status-content",
        "stuck_work": "stuck-content",
        "fetch_recency": "recency-content",
        "coverage": "coverage-content",
        "translation_status": "translation-stats-content",
        "priority_breakdowns": "priority-stats-content",
    }

    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = db_path
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
                    yield from self._compose_chunk(name)
            with Vertical(id="stats-right-col"):
                yield Label(
                    f"[b]{self.LABELS[self.TAG_METRIC]}[/b]",
                    id=f"stats-label-{self.TAG_METRIC}",
                    classes="stats-header",
                )
                with VerticalScroll(id="tag-stats-scroll"):
                    yield DataTable(id="tag-stats-table")
        yield Footer()
        yield Button("Close", id="btn-close-sub-queue")

    def _compose_chunk(self, name: str):
        """One metric's section: a heading, and the one widget only it writes."""
        with Vertical(classes="stats-chunk", id=f"chunk-{name}"):
            yield Label(
                f"[b]{self.LABELS.get(name, name)}[/b]",
                id=f"stats-label-{name}",
                classes="stats-header",
            )
            if name == "app_tracking":
                yield DataTable(id="app-stats-table")
            else:
                yield Static(id=self.CONTENT_IDS[name])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-close-sub-queue":
            self.app.pop_screen()

    def on_mount(self) -> None:
        # Placeholders name every section while it is still computing; the first
        # pass then starts without waiting for the scheduler's first tick.
        for widget_id in self.CONTENT_IDS.values():
            self.query_one(f"#{widget_id}", Static).update("[dim]Computing…[/dim]")
        self._start_due_metrics()
        self.set_interval(self.SCHEDULER_TICK_SECONDS, self._refresh_due_metrics)

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

    def _refresh_due_metrics(self) -> None:
        """Timer callback: let the worker decide what is due."""
        self._start_due_metrics()

    def _stream_metrics(self, names: list[str]) -> None:
        """Worker body: compute each due metric and hand it to the UI as it lands.

        ``iter_metrics`` shares one connection across the pass, but it is a
        generator, so a chunk is applied the moment its own query returns rather
        than when the slowest one does.
        """
        from src.database import compact_tag_ids

        for name, entry in metrics.iter_metrics(self.db_path, names):
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
            f"[b]{self.LABELS.get(name, name)}[/b] [dim]{duration:.1f} ms[/dim]"
        )

    def _set_text(self, name: str, text: str) -> None:
        self.query_one(f"#{self.CONTENT_IDS[name]}", Static).update(text)

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
            if name in self.CONTENT_IDS:
                self._set_text(name, "[dim]unavailable[/dim]")
            elif name in ("app_tracking", "tag_counts"):
                self.query_one(
                    "#app-stats-table" if name == "app_tracking" else "#tag-stats-table",
                    DataTable,
                ).clear(columns=True)
        elif name == "totals":
            self._set_text(
                name,
                f"[b]Live items:[/b] {value.get('alive', 0):,}   "
                f"[b]Dead:[/b] {value.get('dead', 0):,}   "
                f"[dim](total {value.get('total', 0):,})[/dim]",
            )
        elif name == "app_tracking":
            table = self.query_one("#app-stats-table", DataTable)
            table.clear(columns=True)
            table.add_columns("AppID", "Last Page", "Last Cursor")
            for app in value:
                cursor = str(app.get("last_cursor", "") or "")
                table.add_row(
                    str(app.get("appid")),
                    str(app.get("last_page_scanned", 0) or 0),
                    cursor[:30] + "..." if len(cursor) > 30 else cursor,
                )
        elif name == "status_counts":
            lines = [
                f"  Status {row.get('status')}: {row.get('count', 0):,}"
                for row in value
            ]
            self._set_text(
                name,
                "[b]Record count by status[/b]\n" + ("\n".join(lines) or "  (none)"),
            )
        elif name == "stuck_work":
            self._set_text(name, self._format_stuck(value))
        elif name == "fetch_recency":
            self._set_text(
                name,
                "[b]Record count by fetch recency[/b]\n"
                f"  Fresh (last {metrics.DEFAULT_STALENESS_DAYS}d): {value.get('fresh', 0):,}\n"
                f"  Stale: {value.get('stale', 0):,}\n"
                f"  Never attempted: {value.get('blank', 0):,}",
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
                table.add_row(str(tag), f"{count:,}")
        elif name == "priority_breakdowns":
            self._set_text(name, self._format_priority(value))

    @staticmethod
    def _format_coverage(cov: dict) -> str:
        """Coverage as progress over live items, not a dump of raw counts."""
        total = cov.get("total", 0) or 0
        if not total:
            return "[dim]No live items to cover.[/dim]"
        stages = (
            ("api_fetched", "API data"),
            ("described", "Description"),
            ("imaged", "Image"),
            ("translated", "Translation"),
            ("attributed", "Creator"),
        )
        lines = [f"[b]Live items:[/b] {total:,}", ""]
        for key, label in stages:
            done = cov.get(key, 0) or 0
            pct = done / total * 100
            filled = int(round(pct / 100 * 20))
            bar = f"[green]{'█' * filled}[/green][dim]{'░' * (20 - filled)}[/dim]"
            lines.append(f"{label:<12} {pct:5.1f}%  {bar}  {done:,} / {total:,}")
        return "\n".join(lines)

    @staticmethod
    def _format_stuck(stuck: dict) -> str:
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
            f"[bold red]{total:,} dead item(s) are still flagged in a work queue[/bold red]",
            "",
        ]
        for key, label in labels:
            lines.append(f"  {label}: {stuck.get(key, 0) or 0:,}")
        lines.append("\n[dim]These rows can never complete; the queues will not drain.[/dim]")
        return "\n".join(lines)

    @staticmethod
    def _format_priority(breakdowns: dict) -> str:
        """Outstanding work per queue, read as queue state rather than a column dump."""
        labels = {
            "translation_priority": "Translation",
            "needs_image": "Image",
            "needs_web_scrape": "Web scrape",
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
            bucket_val = self.query_one("#analysis-bucket-size", Input).value
            self.bucket_days = max(1, int(bucket_val or "7"))
        except ValueError:
            self.bucket_days = 7

        result = view_window_analysis(self.db_path, bucket_days=self.bucket_days)
        table = self.query_one("#analysis-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Age Range", "Items", "Median Views", "P10", "P90", "Relative")

        max_median = max((b["median"] for b in result["buckets"]), default=1)

        for b in result["buckets"]:
            bar_len = int(b["median"] / max(max_median, 1) * 40)
            bar = "█" * bar_len
            table.add_row(
                f"{b['age_start']}-{b['age_end']}d",
                str(b["count"]),
                str(b["median"]),
                str(b["p10"]),
                str(b["p90"]),
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
                yield RichLog(id="dm-log-view", auto_scroll=True, wrap=True, max_lines=200)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#dm-controls").styles.width = 20
        self._update_status()
        # Fill the pane on first paint, then keep polling. The old `tail -f`
        # subprocess is gone: it read the log a line at a time, which is too slow
        # for a production log. `tail_log` reads at most TAIL_BYTES per call, the
        # same bounded call the web panel makes.
        self._poll_tail()
        self._log_timer = self.set_interval(2.0, self._poll_tail)

    def on_unmount(self) -> None:
        # The screen is gone; stop asking the controller for log lines.
        if self._log_timer is not None:
            self._log_timer.stop()
            self._log_timer = None

    def _daemon_is_running(self) -> bool:
        return self.controller.is_running()

    def _update_status(self) -> None:
        status = self.controller.status()
        if status["running"]:
            self.query_one("#dm-status", Static).update(f"[green]Running (PID: {status['pid']})[/green]")
        else:
            self.query_one("#dm-status", Static).update("[red]Not running[/red]")

    def _read_pid(self) -> int | None:
        return self.controller.read_pid()

    def _start_daemon(self) -> bool:
        changed, message = self.controller.start()
        if not changed and message.startswith("Already running"):
            self.query_one("#dm-status", Static).update(f"[green]{message}[/green]")
        return changed

    def _stop_daemon(self) -> bool:
        self.controller.stop()
        return True

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
            view.write(line)
        self._log_offset = result.get("offset", self._log_offset)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dm-start":
            self._start_daemon()
            self._update_status()
        elif event.button.id == "dm-stop":
            self._stop_daemon()
            self._update_status()
        elif event.button.id == "dm-restart":
            self.controller.restart()
            self._update_status()
        elif event.button.id == "dm-close":
            self.app.pop_screen()


class SubscriptionQueueScreen(ModalScreen):
    """A modal screen that displays the subscription queue with clickable links."""

    def __init__(self, db_path: str, pause_lock_file: str):
        super().__init__()
        self.db_path = db_path
        self.pause_lock_file = pause_lock_file

    def on_mount(self) -> None:
        """Create the pause lock file when the screen is mounted."""
        try:
            with open(self.pause_lock_file, "w") as f:
                pass # Create the file
        except Exception as e:
            logging.error(f"Failed to create pause lock file: {e}")

    def on_unmount(self) -> None:
        """Remove the pause lock file when the screen is unmounted."""
        try:
            if os.path.exists(self.pause_lock_file):
                os.remove(self.pause_lock_file)
        except Exception as e:
            logging.error(f"Failed to remove pause lock file: {e}")

    def compose(self) -> ComposeResult:
        from rich.text import Text as RichText
        with Vertical(id="sub-queue-container"):
            yield Label("Subscription Queue", id="sub-queue-title")
            
            items = get_queued_items(self.db_path)
            if not items:
                yield Label("Queue is empty. Press 's' on an item to add it.")
            else:
                for item in items:
                    wid = item['workshop_id']
                    title = item['title']
                    url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}"
                    link_text = RichText.from_markup(f"[link={url}]{url}[/link] : {title}")
                    yield Static(link_text)
            
            yield Button("Close", id="btn-close-sub-queue")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-close-sub-queue":
            self.app.pop_screen()

def load_tui_state(path: str) -> dict:
    """Loads the TUI state from a YAML file."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

def save_tui_state(path: str, state: dict) -> None:
    """Saves the TUI state to a YAML file."""
    try:
        with open(path, 'w', encoding='utf-8') as f:
            yaml.dump(state, f, default_flow_style=False)
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

    def compose(self) -> ComposeResult:
        with Horizontal(id="details-buttons-row"):
            with Horizontal(id="top-left-buttons"):
                yield Button("Queue", id="btn-queue-sub", classes="details-button")
                yield Button("Unqueue", id="btn-unqueue-sub", classes="details-button")
                yield Button("Show Original", id="btn-toggle-translation", classes="details-button")
            yield Button("jump", id="btn-jump-author", variant="primary")

        with Horizontal(id="title-creator-row"):
            yield Label("", id="item-title")
            yield Label("", id="item-creator")
        
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
                yield Label("Subs: N/A", id="stat-subs")
                yield Label("Favs: N/A", id="stat-favs")

        desc_container = Vertical(
            Markdown(id="detail-content"),
            id="desc-container"
        )
        desc_container.border_title = "Description"
        yield desc_container

    def on_mount(self) -> None:
        """Setup background refresh to catch translation updates."""
        self.set_interval(2.0, self.refresh_data)

    async def refresh_data(self) -> None:
        """Fetches fresh data from DB for the current workshop_id."""
        if self.workshop_id:
            # We access db_path via self.app (ScraperApp instance)
            fresh_data = get_item_details(self.app.db_path, self.workshop_id)
            if fresh_data:
                self.item_data = fresh_data

    async def watch_workshop_id(self, workshop_id: int) -> None:
        """When the pane adopts an item, apply detail priority once and fetch it.

        The bump lives here rather than in the list-view highlight handler so it
        fires on pane load, not on every highlight event. The 2-second
        refresh_data poll below stays read-only on purpose: re-applying detail
        priority there would re-queue whatever is on screen indefinitely.
        """
        self.item_data = None
        if workshop_id:
            db_path = self.app.db_path
            bump_web_priority_for_detail(db_path, workshop_id)
            bump_translation_for_detail(db_path, workshop_id)
            bump_image_priority_for_detail(db_path, workshop_id)
            bump_api_priority_for_detail(db_path, workshop_id)
            await self.refresh_data()

    def watch_item_data(self, item_data: dict) -> None:
        self.update_content()

    def watch_show_translated(self, show_translated: bool) -> None:
        self.update_content()

    def update_content(self) -> None:
        if not self.item_data:
            self.query_one("#detail-content", Markdown).update("Select an item to see details.")
            self.query_one("#item-title", Label).update("")
            self.query_one("#item-creator", Label).update("")
            self.query_one("#btn-toggle-translation").display = False
            self.query_one("#btn-jump-author").display = False
            self.query_one("#btn-queue-sub").display = False
            self.query_one("#btn-unqueue-sub").display = False
            
            for stat in ["id", "created", "updated", "tags", "size", "views", "subs", "favs"]:
                self.query_one(f"#stat-{stat}", Label).display = False
            self.query_one("#wilson-scores", Label).update("")
            return

        item = self.item_data
        
        is_queued = bool(item.get("is_queued_for_subscription", 0))
        self.query_one("#btn-queue-sub").display = not is_queued
        self.query_one("#btn-unqueue-sub").display = is_queued
        
        display_translated = self.show_translated and item.get("translate_version")
        title = item.get("title_en") if display_translated and item.get("title_en") else item.get("title", "N/A")
        
        creator_name = item.get("personaname_en") if display_translated and item.get("personaname_en") else item.get("personaname")
        if not creator_name:
            creator_name = str(item.get("creator", "N/A"))
            
        self.query_one("#item-title", Label).update(f"[b]{title}[/b]")
        self.query_one("#item-creator", Label).update(creator_name)
        
        jump_btn = self.query_one("#btn-jump-author", Button)
        if item.get("creator"):
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

        for stat in ["id", "created", "updated", "tags", "size", "views", "subs", "favs"]:
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
        
        self.query_one("#stat-tags", Label).update(f"[b]Tags:[/b] {', '.join(tags_list) if tags_list else 'None'}")
        self.query_one("#stat-size", Label).update(f"[b]Size:[/b] {format_size(item.get('file_size'))}")
        self.query_one("#stat-views", Label).update(f"[b]Views:[/b] {format_count(item.get('views', 0))}")
        
        subs_current = format_count(item.get('subscriptions', 0))
        subs_lifetime = format_count(item.get('lifetime_subscriptions', 0))
        self.query_one("#stat-subs", Label).update(f"[b]Subscribers:[/b] {subs_current} / {subs_lifetime}")
        
        favs_current = format_count(item.get('favorited', 0))
        favs_lifetime = format_count(item.get('lifetime_favorited', 0))
        self.query_one("#stat-favs", Label).update(f"[b]Favorites:[/b] {favs_current} / {favs_lifetime}")

        wilson_label = self.query_one("#wilson-scores", Label)
        app = self.app
        cutoffs = getattr(app, '_wilson_cutoffs', {}) if app else {}
        wilson_label.update(self._format_wilson_scores(item, cutoffs))
        wilson_label.display = bool(item.get("wilson_favorite_score") is not None)

        md_content = bbcode_to_markdown(desc)
        if item.get("translation_priority", 0) > 0 and not item.get("translate_version"):
             md_content = f"> *[yellow]Translation requested, currently in queue...[/yellow]*\n\n{md_content}"
             
        self.query_one("#detail-content", Markdown).update(md_content)

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
    _TICK_MODULUS = len(BRAILLE) * max(m for _s, m, _c in pending.STAGES)

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

    def compose(self) -> ComposeResult:
        wid = self.item_data.get("workshop_id", "N/A")
        title = self.item_data.get("title_en") or self.item_data.get("title", "Unknown Title")
        creator = self.item_data.get("personaname_en") or self.item_data.get("personaname") or self.item_data.get("creator", "Unknown Creator")
        is_queued = self.item_data.get("is_queued_for_subscription", 0)
        prefix = "[green]*[/green] " if is_queued else "  "
        spin = self._spinner()

        yield Label(f"{prefix}[b]{title}[/b] ({wid})")
        yield Label(f"  By: {creator}   {spin}")

    async def refresh_item(self) -> None:
        """Re-compose the item to reflect any changes in item_data."""
        await self.recompose()


class SearchRow(Horizontal):
    """A single row in the search builder."""
    def __init__(self, fields: list[str], field_ops_map: dict, is_first: bool = False, initial_filter: dict = None):
        super().__init__(classes="search-row")
        self.fields = fields
        self.field_ops_map = field_ops_map  # {field_name: [op_names]}
        self.is_first = is_first
        self.initial_filter = initial_filter or {}

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
        
        val = self.initial_filter.get("value", "")
        yield Input(placeholder="Value", id="value-input", classes="row-input", value=val)
        
        yield Button("AND", id="btn-and", variant="default", classes="row-btn")
        yield Button("OR", id="btn-or", variant="default", classes="row-btn")
        if not self.is_first:
            yield Button("X", id="btn-remove", variant="error", classes="row-btn-remove")
        else:
            # Placeholder to keep alignment
            yield Static("", classes="row-btn-remove")

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

    def on_input_blurred(self, event: Input.Blurred) -> None:
        if event.control.id == "value-input":
            self._clamp_percentile()

    def _clamp_percentile(self) -> None:
        op_select = self.query_one("#op-select", Select)
        if op_select.value != "percentile":
            return
        try:
            inp = self.query_one("#value-input", Input)
            v = int(float(inp.value))
            v = max(0, min(99, v))
            inp.value = str(v)
        # Empty or non-numeric user input is left exactly as typed; clamping happens
        # once it parses.
        except (ValueError, TypeError):
            pass

class SearchBuilder(VerticalScroll):
    """A container for multiple SearchRows."""
    def compose(self) -> ComposeResult:
        self.fields = ALL_FILTER_FIELDS
        # field_name -> [ops] lookup from the central schema
        self.field_ops = {f["field"]: f["ops"] for f in FILTER_SCHEMA}
        yield SearchRow(self.fields, self.field_ops, is_first=True)

    def add_row(self, logic: str) -> None:
        new_row = SearchRow(self.fields, self.field_ops)
        self.mount(new_row)
        new_row.logic = logic

    def set_filters(self, filters: list[dict]) -> None:
        """Populates the builder with a given list of filters."""
        for row in list(self.query(SearchRow)):
            row.remove()
            
        if not filters:
            self.mount(SearchRow(self.fields, self.field_ops, is_first=True))
            return

        for i, f in enumerate(filters):
            is_first = (i == 0)
            row = SearchRow(self.fields, self.field_ops, is_first=is_first, initial_filter=f)
            if not is_first:
                row.logic = f.get("logic", "AND")
            self.mount(row)

    def get_filters(self) -> list[dict]:
        filters = []
        rows = self.query(SearchRow)
        for i, row in enumerate(rows):
            op = row.query_one("#op-select", Select).value
            val = row.query_one("#value-input", Input).value
            field = row.query_one("#field-select", Select).value
            if not isinstance(field, str) or not isinstance(op, str) or not field.strip() or not op.strip():
                continue  # skip unconfigured rows (Sentinel.BLANK etc.)
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
            "Clear Pending Database",
            self.app.action_clear_pending,
            help="Remove all unscraped/pending items from the database",
        )
        yield DiscoveryHit(
            "Show Subscription Queue",
            self.app.action_show_sub_queue,
            help="Show items queued for subscription as clickable links",
        )

    async def search(self, query: str) -> Iterable[Hit]:
        """Search for database commands matching the query."""
        matcher = self.matcher(query)
        
        commands = {
            "Clear Pending Database": self.app.action_clear_pending,
            "Show Subscription Queue": self.app.action_show_sub_queue,
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

class ScraperApp(App):
    """A Terminal GUI for searching the Steam Workshop database."""

    COMMANDS = {SystemCommandsProvider, DatabaseCommands}

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+d", "show_daemon", "Daemon"),
        ("ctrl+r", "show_stats", "Stats"),
        ("s", "toggle_queue", "Queue for Sub"),
        ("l", "show_sub_queue", "List Queued Items"),
        ("ctrl+s", "save_filter_for_scraper", "Save Filter"),
        ("ctrl+w", "toggle_translation", "Toggle Translation"),
        ("ctrl+a", "add_and_row", "AND"),
        ("ctrl+o", "add_or_row", "OR"),
        ("ctrl+x", "delete_bottom_row", "Delete Row"),
        ("ctrl+question_mark", "show_analysis", "Analysis"),
        ("ctrl+b", "subscribe", "Subscribe"),
    ]

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
    .stats-chunk {
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
    #top-left-overlay Button, .top-right-btn {
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
    .sort-select { width: 60%; }
    .sort-order { width: 40%; }

    #details-container {
        width: 60%;
        border: solid $secondary;
        padding: 0 1;
        margin: 0;
        layout: vertical;
    }
    #item-details {
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
    #sub-queue-container {
        width: 80%;
        height: 80%;
        background: $surface;
        border: thick $primary;
        padding: 1;
    }
    #sub-queue-title {
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
        initialize_database(self.db_path)
        self._wilson_cutoffs = {}
        self._web_port = None
        # One controller for the process: the TUI screen and the embedded web
        # server both drive the daemon through it, so a start from either UI is
        # visible to the other.
        self._daemon_controller = DaemonController(self.config_path, config=self.config)
        self._start_webserver()
        self.current_item_creator = None
        self.pause_lock_file = ".pauselock"
        
        # Pagination state
        self.current_offset = 0
        self.has_more_results = True
        self.is_loading = False
        self.is_single_creator_mode = False
        # Filters that the last "Jump to Author" replaced, kept in memory so the
        # Return button can put them back without depending on a state file read.
        self._pre_jump_filters: list[dict] | None = None
        
        # UI State recovery
        # We use a hidden file to avoid cluttering the working directory
        self.state_file = ".tui_state.yaml"
        self._initial_state = load_tui_state(self.state_file)
        self._restored_scroll_y = self._initial_state.get("scroll_y", 0)
        self._restored_selected_id = self._initial_state.get("selected_workshop_id", None)
        self._has_restored_state = False

    def save_state(self) -> None:
        """Saves current UI state to disk."""
        if not self.is_mounted or not self._has_restored_state or self.is_single_creator_mode:
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
                "scroll_y": list_view.scroll_y,
                "selected_workshop_id": selected_id
            }
            save_tui_state(self.state_file, state)
        except Exception as exc:
            pass
            logging.debug("TUI state save skipped: %s", exc)

    def on_mount(self) -> None:
        """Initialize the UI and recover state."""
        self.query_one("#btn-return", Button).display = False
        # Recover sorting and filters
        if self._initial_state:
            try:
                if "sort_by" in self._initial_state:
                    self.query_one("#sort-by", Select).value = self._initial_state["sort_by"]
                if "sort_order" in self._initial_state:
                    self.query_one("#sort-order", Select).value = self._initial_state["sort_order"]
                if "filters" in self._initial_state:
                    builder = self.query_one("#search-builder", SearchBuilder)
                    builder.set_filters(self._initial_state["filters"])
            except Exception:
                logging.debug("Failed to restore filter state from initial load")
                pass
                
        self.call_after_refresh(self.execute_search)
        
        # Watch the scroll_y property to trigger infinite loading
        list_view = self.query_one("#results-list", ListView)
        self.watch(list_view, "scroll_y", self._check_scroll_bottom)

        # Animate braille spinner on pending items
        self.set_interval(0.15, self._tick_spinners)

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

    def compose(self) -> ComposeResult:
        yield Header()
        
        search_builder = SearchBuilder(id="search-builder")
        search_container = Vertical(
            search_builder,
            Horizontal(
                Button("Execute Search", id="btn-execute-search", variant="primary"),
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
        ]
        
        sort_container = Horizontal(
            Select(sort_options, value="title", id="sort-by", classes="sort-select"),
            Select([("ASC", "ASC"), ("DESC", "DESC")], value="ASC", id="sort-order", classes="sort-order"),
            id="sort-container"
        )
        sort_container.border_title = "Sort"

        results_list = ListView(id="results-list")
        results_list.border_title = "Items"

        compact_buttons = Horizontal(
            Button("Fetch New", id="btn-fetch-new", classes="compact-btn"),
            Button("Upd.Vis", id="btn-update-visible", classes="compact-btn"),
            id="compact-buttons"
        )

        details_container = Vertical(
            DetailsPane(id="item-details"),
            id="details-container"
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
        pass  # search only on explicit "Execute Search" click

    async def on_select_changed(self, event: Select.Changed) -> None:
        # Save state when sort/filter changes, but don't auto-search
        if event.value is not None and self._has_restored_state:
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
        self._wilson_cutoffs = compute_wilson_cutoffs(self.db_path, filters)

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
            offset=self.current_offset
        )
        
        list_view = self.query_one("#results-list", ListView)
        
        items = [WorkshopItem(item) for item in results]
        await list_view.mount(*items)

        for item in results:
            if item.get("needs_web_scrape", 0) > 0:
                bump_web_priority_for_list(self.db_path, item["workshop_id"])
                bump_translation_for_list(self.db_path, item["workshop_id"])
            if item.get("needs_image", 0) > 0:
                bump_image_priority_for_list(self.db_path, item["workshop_id"])
            bump_api_priority_for_list(self.db_path, item["workshop_id"])
            
        self.current_offset += len(results)
        
        if len(results) < 50:
            self.has_more_results = False
            
        self.is_loading = False

        if not self._has_restored_state:
            self._has_restored_state = True
            
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
                self.current_item_creator = item_data.get('creator')
                # Detail priority is applied by DetailsPane when it adopts the
                # item, not here: this handler fires on every highlight move.
                detail_pane = self.query_one("#item-details", DetailsPane)
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
            with open('.fetch_new', 'w') as f:
                f.write('1')
            self.notify("Fetch-new triggered! The daemon will scan recently-updated items on its next cycle.")
        elif event.button.id == "btn-update-visible":
            await self.action_update_visible()
        elif event.button.id in ("btn-queue-sub", "btn-unqueue-sub"):
            await self.action_toggle_queue()
        elif event.button.id == "btn-execute-search":
            await self.execute_search()
        elif event.button.id == "btn-save-filter":
            await self.action_save_filter_for_scraper()
        elif event.button.id in ("btn-and", "btn-or"):
            logic = "AND" if event.button.id == "btn-and" else "OR"
            self.query_one("#search-builder", SearchBuilder).add_row(logic)
        
        elif event.button.id == "btn-remove":
            row = event.button.parent
            if isinstance(row, SearchRow):
                row.remove()

        elif event.button.id == "btn-return":
            self.action_return_from_creator()

        elif event.button.id == "btn-jump-author" and self.current_item_creator:
            # Save state before switching to single creator mode
            if not self.is_single_creator_mode:
                self.save_state()
                # Snapshot what the jump is about to throw away so Return can
                # restore it. This is in memory on purpose: the on-disk snapshot
                # above is for a restart, and a later state write could overwrite
                # it before the user presses Return.
                self._pre_jump_filters = self.query_one(
                    "#search-builder", SearchBuilder
                ).get_filters()

            self.is_single_creator_mode = True
            self.query_one("#btn-save-filter", Button).display = False
            self.query_one("#btn-return", Button).display = True

            builder = self.query_one("#search-builder", SearchBuilder)

            # Clear all current rows
            await builder.query(SearchRow).remove()

            # Build the row already carrying the author filter. Assigning the
            # Selects after mount used to race the field's Change handler, which
            # is what populates the op list: "is" was rejected for the default
            # text field before that handler ran.
            new_row = SearchRow(
                builder.fields,
                builder.field_ops,
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
                self.run_worker(self.execute_search())

            self.call_after_refresh(setup_author_filter)

        elif event.button.id == "btn-toggle-translation":
            self.action_toggle_translation()

    def action_return_from_creator(self) -> None:
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

        self.is_single_creator_mode = False
        self.query_one("#btn-save-filter", Button).display = True
        self.query_one("#btn-return", Button).display = False

        if filters is None:
            self.save_state()
            return

        builder = self.query_one("#search-builder", SearchBuilder)
        builder.set_filters(filters)

        def after_restore() -> None:
            self.save_state()
            self.run_worker(self.execute_search())

        self.call_after_refresh(after_restore)

    async def action_save_filter_for_scraper(self) -> None:
        builder = self.query_one("#search-builder", SearchBuilder)
        filters = builder.get_filters()
        current_appid = self.config.get("daemon", {}).get("target_appids", [None])[0]

        if current_appid is None:
            self.notify("No target AppID configured for saving filter.", severity="error")
            return

        save_app_filter(self.db_path, current_appid, enrichment_filters=json.dumps(filters))
        self.notify(f"Filter saved for AppID {current_appid}. Scraper will use this for enrichment.")

    def action_toggle_translation(self) -> None:
        detail_pane = self.query_one("#item-details", DetailsPane)
        detail_pane.show_translated = not detail_pane.show_translated
        
    async def action_toggle_queue(self) -> None:
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
        toggle_subscription_queue_status(self.db_path, workshop_id)
        
        # Update UI state in place
        item.item_data["is_queued_for_subscription"] = not item.item_data.get("is_queued_for_subscription", 0)

        # Refresh the ListItem to show the change
        await item.refresh_item()

        # Update details pane if it's showing the same item
        detail_pane = self.query_one("#item-details", DetailsPane)
        if detail_pane.workshop_id == workshop_id and detail_pane.item_data is not None:
            detail_pane.item_data["is_queued_for_subscription"] = item.item_data["is_queued_for_subscription"]
            detail_pane.update_content()
        # Move to next item
        if list_view.index < len(list_view) - 1:
            list_view.index += 1
        
        # Scroll to keep highlight visible if needed
        # list_view.scroll_to_widget(item)

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

    def action_clear_pending(self) -> None:
        """Removes all unscraped/pending items from the database."""
        count = clear_pending_items(self.db_path)
        self.notify(f"Database cleared: {count} pending items removed.")
        self.run_worker(self.execute_search())

    def action_show_stats(self) -> None:
        """Shows the database statistics screen."""
        self.push_screen(StatsScreen(self.db_path))
        
    async def action_update_visible(self) -> None:
        """Queues all visible list items for API re-fetch (priority 10)."""
        try:
            list_view = self.query_one("#results-list", ListView)
        except Exception:
            return

        # Compute viewport-visible items from scroll position
        scroll_y = list_view.scroll_y
        visible_h = list_view.size.height
        child_h = 2  # each WorkshopItem is 2 lines
        visible_ids = []
        for i, child in enumerate(list_view.children):
            if hasattr(child, 'item_data') and child.item_data:
                top = i * child_h
                bot = top + child_h
                if bot > scroll_y and top < scroll_y + visible_h:
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
        # Same guard as the web route: an item known to be gone (status -1) must
        # not be put back in the API fetch queue by a bulk update.
        conn.execute(
            f"UPDATE workshop_items SET api_priority = 10 WHERE workshop_id IN ({placeholders}) "
            "AND (status IS NULL OR status != -1)",
            visible_ids,
        )
        conn.commit()
        conn.close()
        self.notify(f"Queued {len(visible_ids)} items for update.")

    def action_show_sub_queue(self) -> None:
        """Shows the subscription queue modal screen."""
        self.push_screen(SubscriptionQueueScreen(self.db_path, self.pause_lock_file))

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
            detail = self.query_one("#item-details", DetailsPane)
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
                self.notify(data.get("message", "Subscribe failed."), severity="error")
        except Exception as e:
            self.notify(f"Subscribe request failed: {e}", severity="error")

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
        # `src/daemon_runner.py`.
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        
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

    app = ScraperApp(config_path)
    app.run()

if __name__ == "__main__":
    main()
