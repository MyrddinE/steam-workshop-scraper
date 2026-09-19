import time
import math
import signal
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import NamedTuple
from src.database import (
    get_next_items_to_fetch, 
    insert_or_update_item, 
    count_never_fetched_items, 
    count_fetchable_items, 
    insert_or_update_creator, 
    get_creator, 
    get_app_tracking,
    update_app_tracking_cursor,
    save_enrichment_filters,
    get_connection,
    get_item_details,
    normalize_tags,
    _evaluate_filters,
    get_enrichment_filters,
    USER_PRIORITY_FLOOR,
    WORKSHOP_ITEM_COLUMNS,
)
from src.steam_api import (
    get_workshop_details,
    get_workshop_details_batch,
    query_workshop_items,
    get_player_summaries,
    query_workshop_newest_page,
    set_api_delay,
    query_workshop_updated_page,
    STEAM_API_MAX_IDS_PER_REQUEST,
)
from src.translator import TranslatorThread, is_ascii
from src.config import config_value_with_legacy, login_secure_value, save_config
from src.database import raise_web_scrape_priority, queue_field_for_translation, raise_image_priority, translation_is_current
from src.firefox_cookies import steam_login_secure
from src.web_worker import WebScraperThread
from src.image_worker import ImageDownloadThread
from src.backup import BackupThread
from src.daemon_state import StateStore, state_path_for
from src import pacing
from src import images
from src import activity
from src import capture
from src import crash
from src import session_health
from src.subscription_sync import reconcile_own_subscriptions
from src.workshop_folders import WorkshopFolders, DOWNLOADED_ITEM_SCAN_INTERVAL_SECONDS

# API statuses the fetch path has an explicit branch for. Anything else is
# captured as evidence and then treated as temporary by _settle_api_failure; it
# is never silently persisted as a success.
HANDLED_API_STATUSES = frozenset({200, 404, 500})

# The only API outcome that cannot succeed on retry. Everything else -- 500,
# transport exceptions (which get_workshop_details reports as 500), and any
# status without its own branch -- is retried at one priority level lower.
PERMANENT_API_STATUSES = frozenset({404})


# --- API request backoff -----------------------------------------------------
# The delay is a property of the *request*, not of the items it carried. One
# batched GetPublishedFileDetails call returns up to
# STEAM_API_MAX_IDS_PER_REQUEST results, so per-item signals are the wrong unit:
# a single overloaded call would be diluted by the results that were fine, and a
# batch of "not found" results -- a perfectly successful call -- would read as a
# run of failures. Only the request outcome moves the delay; per-item results
# drive item state (fetch_status, priority, queue flags, death) and nothing else.
#
# The shape is TCP congestion control, not a safety net: every refusal doubles
# the delay and healthy operation walks it back down, so the client converges on
# the fastest rate Steam will sustain -- a limit that is not published and may
# move. Steady state is a sawtooth around that rate.
#
# The walk back down is measured in *time*, not in successful requests: the
# delay halves for every `pacing.HALF_LIFE_SECONDS` of healthy operation, so it
# recovers over the same wall-clock window whatever its current size. A count
# would not do that -- one successful request is a different amount of time at
# every delay -- and it is the reason all three pacing workers share
# `src/pacing.py` instead of each keeping its own arithmetic.
#
# There is no ceiling. One was kept while the back-off could still be moved by
# the wrong signal, but it also caps convergence, and an uncapped delay cannot
# run away: it only doubles when an attempt fails, and the next attempt is a
# whole delay away, so the delay after k refusals is d0 * 2**k and the elapsed
# time to reach it is d0 * (2**k - 1). The delay tracks the length of the outage
# rather than outrunning it.
#
# `api_delay` is a literal inter-call delay, not a target rate. It is added on
# top of the request's own latency and is normally the smaller term; the
# production value (0.01 s) exists to add nothing, not to target 100 req/s.
#
# Batch size is deliberately NOT folded into the delay. A refusal costs one
# multiplication whatever ids it carried, so if Steam meters the limit per item
# rather than per call, the converged *call* delay settles at roughly N times
# the per-item cost and the item throughput is the same as without batching; if
# Steam meters per call, the converged item throughput simply rises with the
# batch, which is the point of batching. Scaling by batch size would have to
# guess the cost model and buys nothing: the feedback loop finds whichever limit
# is real.

# The delay never drops below this -- a zero delay is not a rate limit and a
# negative one is nonsense.
API_DELAY_FLOOR = 0.01

# The staleness sweep is a full-table UPDATE whose threshold is measured in days
# (`item_staleness_days`, 60 in production). Running it on every batch -- every
# few seconds -- scans the table thousands of times for a decision that changes
# at most once per item per threshold period. Hourly bounds the promotion lag to
# well under 1% of a 60-day threshold while cutting the scan by ~720x at the
# observed batch cadence. The sweep still runs on the first batch after startup,
# so a long-idle daemon does not sit on a stale queue.
STALE_SWEEP_INTERVAL_SECONDS = 3600

# How much fetchable work `seed_database` wants outstanding. It is both the
# guard threshold -- a pass returns at once while the queue is already at it --
# and the per-run fill target the cursor loop stops at.
#
# It is 200 because the fetch loop drains the queue between discovery passes.
# The earlier target of 100 was small enough that every pass found the queue
# already at or above it and skipped, so the refill raced the drain instead of
# leading it: the buffer was a level the drain kept crossing, not headroom above
# it. 200 sits clear of that crossing, and at the request page size of 100 it is
# two pages of fresh items per pass.
DISCOVERY_FILL_TARGET = 200

# How long the discovery thread waits between passes. It is a check interval, not
# a rate: `seed_database` returns at once while the fetchable queue is already at
# its target, so the nap costs nothing and waking often keeps the queue topped up
# as the fetch loop drains it.
DISCOVERY_IDLE_SECONDS = 30.0

# The owner's subscriptions are reconciled once per appid at startup and then on
# this cadence. Daily is the right order for it: the list only moves when a
# human subscribes or unsubscribes, the one moment that matters (a subscribe the
# userscript confirmed) is stamped immediately by /api/subscribed, and a walk is
# a handful of page fetches against Steam's budget. Like the staleness sweep,
# this is a housekeeping task on the per-batch path, so it runs on the first
# batch and is guarded by a monotonic interval afterwards.
SUBSCRIPTION_RECONCILE_INTERVAL_SECONDS = 86400

# ...unless the walk could not authenticate. Then the cadence above is the wrong
# clock, because what was wrong is a credential the operator can renew in a
# browser at any moment, and the daily walk has already happened for the day: the
# markers would stay wrong for another day after they had already fixed it. A
# retry on this interval costs no request while the token is still expired -- the
# check is local, and re-reading the browser's store is a file copy -- and one
# walk as soon as it is not, which is the first one that can succeed.
SUBSCRIPTION_RECONCILE_RETRY_SECONDS = 900


def user_requested_priority(inherited_priority: int) -> int:
    """The part of a pre-fetch ``api_priority`` a person asked for, else 0.

    Dependent stages inherit *this* rather than the whole value. Inheriting the
    whole value made the discovery priority (3) behave like a request, which put
    every newly discovered item the enrichment filters excluded into the same
    queue band as the ones they selected -- and those filters are the only thing
    that decides which items deserve the work. Migration 21->22 returns the rows
    that mistake wrote to backlog priority; this is what stops it recurring.

    The boundary itself is :data:`src.database.USER_PRIORITY_FLOOR`, because the
    migration that repairs the rows this wrote has to draw the same line.
    """
    return inherited_priority if inherited_priority >= USER_PRIORITY_FLOOR else 0


class ScrapeImageOutcome(NamedTuple):
    """What `_raise_scrape_and_image_priorities` did with one item's dependent work.

    Two facts, because they are not the same question:

    * ``enriched`` -- the item matched its AppID's enrichment filters. This is
      what gates translation and the creator-persona refresh, and it says
      nothing about whether anything was queued.
    * ``queued`` -- at least one of `raise_web_scrape_priority` / `raise_image_priority` was
      actually called. An enriched item whose description is current and whose
      preview needs no attempt matches the filters and queues nothing, and the
      discovery line must say so rather than claim it is ``enriching``.
    """

    enriched: bool
    queued: bool


# --- API merge allow-list ----------------------------------------------------
# Item columns that are owned by the queue-flagging helpers rather than by the
# API merge. They must NOT survive a merge: a stale value would clobber a flag
# just set, or resurrect one whose queue has already drained.
#
# raise_web_scrape_priority / raise_image_priority set their columns explicitly between the
# merge and the insert. translation_priority is different only in timing: it is
# written by queue_field_for_translation, which _queue_translations calls after
# the insert. Carrying the pre-fetch snapshot through the merge would write that
# snapshot back over it, so a translator drain that landed while the API fetch
# was in flight would be undone and leave a priority with no queue row behind
# it. Excluding it leaves the column to the code that owns it.
#
# downloaded_at is local state, not an API field: only src.workshop_folders
# writes it and only the subscription walk clears it. Excluding it keeps the
# merge from carrying it at all -- the column is simply not in the statement --
# so a stray API key under that name can never set the green star.
#
# The per-queue completion clocks (web_scraped_at, image_fetched_at,
# translated_at) are our wall time, written at each stage's success point and by
# nothing else. Excluding them keeps a Steam payload from ever carrying a value
# under one of those names into the merge: the whole point of the columns is
# that they record when *we* did the work, not what Steam said.
MERGE_EXCLUDED_KEYS = frozenset({
    "is_queued_for_subscription",
    "needs_web_scrape",
    "image_extension",
    "needs_image",
    "translation_priority",
    "downloaded_at",
    "web_scraped_at",
    "image_fetched_at",
    "translated_at",
})

# Keys retained from an API merge into the item record:
#   * every real column (WORKSHOP_ITEM_COLUMNS), minus the locally-owned ones
#     above (the queue flags, the download latch, the completion clocks)
#   * "tags", which is no longer a column -- it lives in the workshop_tags
#     junction table -- but is consumed by insert_or_update_item's tag sync and
#     so must survive the merge
# Derived rather than hand-listed: adding a column now updates this automatically.
MERGE_ITEM_KEYS = (WORKSHOP_ITEM_COLUMNS - MERGE_EXCLUDED_KEYS) | {"tags"}

# Keys the merge knows about and drops silently (no "unknown column" log line).
MERGE_IGNORED_KEYS = MERGE_EXCLUDED_KEYS | {"result"}


def wilson_lower(successes: int, trials: int, z: float = 1.96) -> float:
    """Wilson score confidence interval lower bound for a Bernoulli parameter.
    Returns 0.0–1.0; penalizes small sample sizes."""
    if trials == 0:
        return 0.0
    p = min(float(successes) / trials, 1.0)
    z2 = z * z
    denom = 1 + z2 / trials
    numer = p + z2 / (2 * trials) - z * math.sqrt(max(0.0, p * (1 - p) / trials) + z2 / (4 * trials * trials))
    return max(0.0, min(1.0, numer / denom))


class DiscoveryThread(threading.Thread):
    """Keep the fetch queue topped up, off the fetch loop's critical path.

    Discovery used to run *inside* the fetch loop and only once that loop had
    drained the queue to nothing, so the daemon starved: it blocked on paging
    until enough new items appeared, and only then resumed fetching. Batching the
    details calls made fetching fast enough that the stall became a visible share
    of the daemon's time.

    This buys no extra API budget. ``steam_api._rate_limit`` is one schedule
    shared by every caller, so this thread waits its turn exactly as the fetch
    loop does; what it buys is that the queue is refilled *while* the loop is
    still working, so the loop never has to stop.

    It holds no state of its own beyond its loop. The cursor, the page-discovery
    cooldown and ``_cursor_exhausted`` all live on the daemon, and only this
    thread writes them, which is what keeps them safe without a lock. The
    fetch loop reads none of them.
    """

    # ``_page_discovery_eligible`` can run a COUNT over the whole table, and
    # nothing indexes the column it counts. Page discovery is only interesting
    # once the cursor walk has finished or the owner has asked for it, so the
    # count is taken every twentieth pass, or at once when a cheap signal fires.
    PAGE_ELIGIBILITY_EVERY = 20

    def __init__(self, daemon: "Daemon", interval: float = DISCOVERY_IDLE_SECONDS):
        super().__init__(daemon=True)
        self.owner = daemon
        self.interval = interval
        self.running = True
        self._passes = 0

    def run(self) -> None:
        logging.info("Discovery thread started.")
        while self.running and self.owner.running:
            try:
                if self._page_discovery_worth_checking():
                    self.owner._run_page_discovery()
                self.owner.seed_database()
            except Exception as exc:
                # A failed pass is a log line, not a dead thread: the fetch loop
                # goes on draining whatever is already queued.
                logging.warning("Discovery pass failed; will try again: %s", exc)
            finally:
                # Wake the fetch loop if it is idling. A spurious wake costs one
                # empty batch read, which is cheaper than the fetch loop asking
                # the database whether there is work on a timer.
                self.owner._work_available.set()
            self._sleep()

    def _page_discovery_worth_checking(self) -> bool:
        self._passes += 1
        if self.owner._cursor_exhausted or os.path.exists('.fetch_new'):
            return True
        return self._passes % self.PAGE_ELIGIBILITY_EVERY == 0 and \
            self.owner._page_discovery_eligible()

    def _sleep(self) -> None:
        """Nap, staying responsive to shutdown."""
        deadline = time.monotonic() + self.interval
        while self.running and self.owner.running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def stop(self) -> None:
        self.running = False


class Daemon:
    def __init__(self, config: dict, config_path: str = "config.yaml"):
        self.config = config
        self.config_path = config_path
        self.running = True
        
        # Implement default fallbacks
        self.db_path = config.get("database", {}).get("path", "workshop.db")
        self.api_key = config.get("api", {}).get("key", "")
        daemon_config = config.get("daemon", {})
        self.api_batch_size = config_value_with_legacy(
            daemon_config, "api_batch_size", "batch_size", 10, section_name="daemon"
        )
        if daemon_config.get("api_delay_seconds") is None and daemon_config.get("request_delay_seconds") is not None:
            logging.warning(
                "Config key 'request_delay_seconds' is deprecated and still honoured; "
                "rename it to 'api_delay_seconds'."
            )
        self.api_delay = daemon_config.get("api_delay_seconds") or daemon_config.get("request_delay_seconds", 1.5)
        self.item_staleness_days = int(daemon_config.get("item_staleness_days") or 30)
        self.creator_staleness_days = int(
            config_value_with_legacy(
                daemon_config, "creator_staleness_days", "user_staleness_days",
                section_name="daemon",
            ) or 90
        )
        set_api_delay(self.api_delay)
        logging.info(f"API delay={self.api_delay}s, Staleness: item={self.item_staleness_days}d, creator={self.creator_staleness_days}d")

        # Write default config keys if absent
        changed = False
        if "daemon" not in self.config:
            self.config["daemon"] = {}
        for key, val in [("item_staleness_days", self.item_staleness_days),
                          ("creator_staleness_days", self.creator_staleness_days)]:
            if key not in self.config["daemon"]:
                self.config["daemon"][key] = val
                changed = True
        if changed:
            save_config(self.config_path, self.config)
        self.pause_lock_file = ".pauselock"
        
        # Translator thread. It owns a slice of the daemon state file beside the
        # database, so the delay it has backed off to survives a restart rather
        # than beginning again at the base on every one.
        self.translator = TranslatorThread(
            config, state_store=StateStore(state_path_for(self.db_path))
        )

        # Optional database backup into a pull-outbox. The feature defaults to
        # OFF: it is only enabled when both `outbox_dir` (or `backup_dir`) and a
        # positive `backup_interval_seconds` are configured, so turning it on for
        # the live instance is a deliberate switch.
        self.outbox_dir = daemon_config.get("outbox_dir") or daemon_config.get("backup_dir")
        # Debugging switches, not permanent ones: on means keep everything. The
        # image switch is separate because the web switch keeps whole bodies
        # unbounded, and an owner reviewing image metadata should not have to
        # collect pages to do it. Image *failures* need neither switch — the
        # outbox alone is enough, like every other failure capture. The web
        # switch is resolved by `capture.web_download_switch` because the web
        # server process reads the same key for the subscribe route, and the
        # deprecated `capture_web_scrapes` name has to be honoured in both.
        self.capture_web_downloads = capture.web_download_switch(daemon_config)
        self.capture_image_downloads = bool(daemon_config.get("capture_image_downloads", False))
        self.backup_interval_seconds = float(daemon_config.get("backup_interval_seconds") or 0)
        self._backup_worker = None
        if self.outbox_dir and self.backup_interval_seconds > 0:
            self._backup_worker = BackupThread(
                self.db_path, self.outbox_dir, self.backup_interval_seconds)
            logging.info(
                "Database backup enabled: outbox=%s interval=%ss",
                self.outbox_dir, self.backup_interval_seconds)

        # Failure capture rides on the same outbox but needs no interval: it is a
        # no-op unless an outbox directory is configured, like the backup above.
        capture.configure(self.outbox_dir, self.capture_web_downloads,
                          self.capture_image_downloads)
        
        # State variables for the request-level congestion-control delay. The
        # counters are diagnostics; the delay itself is the state that matters.
        self.api_successes = 0
        self.api_failures = 0
        # The last delay written to config, so the per-request decay does not
        # rewrite the file on every request.
        self._persisted_api_delay = self.api_delay
        # In memory only, so a daemon restarted after a day resumes at the delay
        # it had reached rather than treating the downtime as healthy operation.
        self._api_clock = pacing.Clock()

        # Monotonic timestamp of the last staleness sweep; None means "never",
        # so the first batch after startup always sweeps.
        self._last_stale_sweep = None

        # Page-based discovery (sort by update time) — runs once a day when eligible
        self._last_page_discovery = 0

        # Set by the discovery thread when it has run a pass, waited on by the
        # fetch loop when the queue is empty. A signal rather than a poll: the
        # only cheap way to ask "is there work?" is this event, because the count
        # query scans the whole table and nothing indexes api_priority.
        self._work_available = threading.Event()
        self._discovery_thread = None
        self._cursor_exhausted = False
        self._saw_pid_file = False  # set True once PID file is seen; prevents false trigger in tests

        # Enforce required target_appids
        self.target_appids = config.get("daemon", {}).get("target_appids")
        if not self.target_appids or not isinstance(self.target_appids, list):
            raise ValueError("Configuration error: 'daemon.target_appids' must be provided as a list.")

        # Monotonic timestamp of the last subscription reconcile; None means
        # "never", so the first batch after startup reconciles each target appid.
        # See SUBSCRIPTION_RECONCILE_INTERVAL_SECONDS.
        self._last_subscription_reconcile = None

        # The downloaded-star scan: local checks that a subscribed item's folder
        # is on disk, so its marker can turn solid green. The locator resolves
        # the Steam libraries once per process and only re-resolves on a miss,
        # and the scan itself is on a monotonic clock (see
        # DOWNLOADED_ITEM_SCAN_INTERVAL_SECONDS) because the per-batch path runs every
        # few seconds. One startup line says why it is off when it cannot work.
        self.workshop_folders = WorkshopFolders(self.db_path, self.config)
        self.workshop_folders.log_status()
        self._last_download_scan = None
        
        # Setup graceful shutdown
        signal.signal(signal.SIGINT, self.handle_shutdown)
        signal.signal(signal.SIGTERM, self.handle_shutdown)

    def _save_config_value(self, key: str, value):
        """Saves a daemon config key=value to the config file. Usable as callback from threads."""
        if "daemon" not in self.config:
            self.config["daemon"] = {}
        self.config["daemon"][key] = value
        save_config(self.config_path, self.config)

    def _refresh_login_cookie(self) -> bool:
        """Re-read the login cookie from the browser, persisting it if it moved.

        Returns True only when the value actually changed, which is what makes a
        retry worthwhile. The config holds the cookie between refreshes rather
        than re-reading the browser on a timer: an unchanged file copy is pure
        overhead, and a gate-shaped scrape failure is the evidence that the
        configured value has gone stale.

        The cookie itself, though, is short-lived: the deployed
        ``steamLoginSecure`` carried ``exp - iat`` of 86,954 seconds, so Steam
        reissues it about daily and the browser renews it silently on use. A
        caller that runs on its own slow schedule -- the subscription reconcile
        is daily -- must therefore refresh first rather than wait to be told by
        a failure.
        """
        if not self.config.get("session", {}).get("read_firefox_cookies"):
            return False
        current = login_secure_value(self.config)
        fresh = steam_login_secure(refresh=True)
        if not fresh or fresh == current:
            return False
        self.config.setdefault("session", {})["login_secure"] = fresh
        # The refreshed value is the live credential and may differ from the one
        # the config previously held, so the crash reporter is told about it.
        crash.register_secret(fresh)
        save_config(self.config_path, self.config)
        logging.info("Login cookie refreshed from the browser and saved to the config.")
        return True

    def _build_user_record(self, steamid: int, personaname: str) -> dict:
        """Builds a pure user record dict for upsert.

        Queueing a non-ASCII name is a side effect and lives in
        `_store_user_record`, which is the only thing that writes this record:
        keeping the builder pure means a record can be built without deciding
        anything about the queue.
        """
        return {
            "steamid": steamid,
            "personaname": personaname,
            "api_fetched_at": int(time.time())
        }

    def _store_user_record(self, steamid: int, personaname: str) -> None:
        """Upsert a creator profile, then queue a non-ASCII name for translation.

        The write comes **before** the queue call on purpose.
        `queue_field_for_translation` inserts the `translation_queue` row and
        raises `creators.translation_priority` in one transaction, so it needs the
        `creators` row to exist: on a creator's first sighting there is nothing for
        the mirror to land on, and queueing first would leave a queue row whose
        mirror reads 0. Both helpers open their own connection and commit, so
        neither may be called from inside another open write transaction -- no
        caller of this method holds one.

        This is the producer issue 45 restored: before the per-field queue was
        introduced, the (since removed) `get_next_translation_item` scanned
        `creators` by `translation_priority`, so raising the mirror in the record was
        the whole producer. The drain reads `translation_queue` now, so the name
        has to be queued as a field like any other.
        """
        insert_or_update_creator(self.db_path, self._build_user_record(steamid, personaname))
        if not is_ascii(personaname):
            queue_field_for_translation(
                self.db_path, "user", steamid, "personaname_en", personaname, 1)

    def _merge_and_clean_api_data(self, api_data: dict, stored_item: dict, item_id: int, now_ts: int) -> dict:
        """Merges API response into existing data, remaps column names, and filters to allowed keys."""
        merged = stored_item.copy()
        api_data.pop("publishedfileid", None)
        api_data.pop("status", None)
        merged.update(api_data)

        if "creator_app_id" in merged:
            merged["creator_appid"] = merged.pop("creator_app_id")
        if "consumer_app_id" in merged:
            merged["consumer_appid"] = merged.pop("consumer_app_id")
        if "description" in merged:
            merged["short_description"] = merged.pop("description")
        # The Steam API still calls these fields time_created / time_updated;
        # the database columns are now named steam_created_at / steam_updated_at.
        if "time_created" in merged:
            merged["steam_created_at"] = merged.pop("time_created")
        if "time_updated" in merged:
            merged["steam_updated_at"] = merged.pop("time_updated")

        clean = {}
        for k, v in merged.items():
            if k in MERGE_ITEM_KEYS:
                clean[k] = v
            elif k not in MERGE_IGNORED_KEYS:
                value_preview = str(v)[:20] + "..." if len(str(v)) > 20 else str(v)
                logger = logging.info if v is not None and str(v).strip() != "" else logging.debug
                logger(f"Discarding unknown API column: '{k}' with value '{value_preview}' for item {item_id}")

        # Success path only: this helper is called after a usable API payload has
        # arrived, so api_fetched_at means "last SUCCESSFUL content pull".
        clean["api_fetched_at"] = now_ts
        clean["api_priority"] = 0  # mark as fetched, no longer queued
        if "tags" in clean:
            clean["tags"] = normalize_tags(clean["tags"])
        return clean

    def _should_enrich(self, appid: int, item: dict) -> bool:
        """Returns True if the item matches enrichment filters for its AppID.
        Evaluates the stored filter list using the same logic as the TUI search builder."""
        if appid is None or appid not in self.target_appids:
            return True
        app_tracking = get_app_tracking(self.db_path, appid)
        if app_tracking is None:
            return True
        filters = get_enrichment_filters(app_tracking)
        if not filters:
            # Either the AppID has no filters -- so everything matches -- or the
            # stored set could not be read, which must not be taken as "excludes
            # everything". Both mean enrich.
            return True
        return _evaluate_filters(item, filters)

    def handle_shutdown(self, signum, frame):
        """Signals the loop to stop and finishes the current batch safely."""
        logging.warning(f"Received signal {signum}, initiating shutdown...")
        self.running = False
        self.translator.running = False
        if hasattr(self, '_web_worker'):
            self._web_worker.running = False
        if hasattr(self, '_image_worker'):
            self._image_worker.running = False
        if self._backup_worker is not None:
            self._backup_worker.running = False

    def _discovery_alive(self) -> bool:
        """Whether a discovery pass should keep going rather than abandon a wait."""
        return self.running and not self._pid_file_removed()

    def process_batch(self):
        """Process one batch: housekeeping, acquire work, then process each item."""
        self._maybe_promote_stale_items()
        self._maybe_reconcile_subscriptions()
        self._maybe_scan_downloaded_items()

        items_to_fetch = self._acquire_batch()
        if items_to_fetch is None:
            return  # database error, already logged
        if not items_to_fetch:
            self._wait_for_work()
            return

        if not self.running or self._pid_file_removed():
            return

        # One bulk details request for the whole batch (chunked only if the
        # configured api_batch_size exceeds the endpoint ceiling). Each request's
        # outcome drives the backoff; the per-item results below only decide
        # each item's state.
        api_data_by_id = self._fetch_details(items_to_fetch)

        creators_to_refresh = []
        for stored_item in items_to_fetch:
            if not self.running or self._pid_file_removed():
                break
            # Items are still processed in the order the queue returned them;
            # each is matched to its own result by id.
            creator_id = self._process_item(
                stored_item,
                api_data=api_data_by_id.get(stored_item["workshop_id"]),
            )
            if creator_id is not None:
                creators_to_refresh.append(creator_id)

        # Creator personas move from one request per item to one per batch.
        self._refresh_creators(creators_to_refresh)

    def _fetch_details(self, items: list[dict]) -> dict[int, dict]:
        """Fetch details for a batch in as few requests as the API allows.

        This is the only place request-level outcomes are counted: one call to
        `_back_off_api_delay` or `_decay_api_delay` per POST.
        A request that fails transports, times out, returns an HTTP error or an
        unparseable body settles every id it carried as a temporary 500; a
        request that returns and parses is a success whatever the individual
        results say.
        """
        api_data_by_id: dict[int, dict] = {}
        for start in range(0, len(items), STEAM_API_MAX_IDS_PER_REQUEST):
            chunk = items[start:start + STEAM_API_MAX_IDS_PER_REQUEST]
            ids = [row["workshop_id"] for row in chunk]
            results = get_workshop_details_batch(ids, self.api_key)
            if results is None:
                self._back_off_api_delay()
                for item_id in ids:
                    api_data_by_id[item_id] = {"status": 500, "publishedfileid": item_id}
            else:
                self._decay_api_delay()
                for item_id in ids:
                    # The batch helper fills omitted ids in as 404, so this
                    # fallback only covers an unanticipated response shape.
                    api_data_by_id[item_id] = results.get(
                        item_id, {"status": 404, "publishedfileid": item_id})
        return api_data_by_id

    def _maybe_promote_stale_items(self) -> None:
        """Run the staleness sweep at most once per STALE_SWEEP_INTERVAL_SECONDS.

        The sweep is a full-table UPDATE, so it cannot sit on the per-batch path
        at a threshold measured in days. A monotonic clock is used because this
        is an elapsed-time interval and must not be moved by wall-clock jumps.
        """
        now = time.monotonic()
        if (self._last_stale_sweep is not None
                and now - self._last_stale_sweep < STALE_SWEEP_INTERVAL_SECONDS):
            return
        self._last_stale_sweep = now
        self._promote_stale_items()

    def _reconcile_interval(self) -> float:
        """How long to wait before the next walk, given how the last one went.

        Daily normally. After a walk that could not authenticate, the short
        retry instead: the daily walk has already run for this day, so waiting
        the full interval again would hold the markers wrong for a day *after*
        the operator fixed the login the banner told them about.

        The recorded problem is the signal, which is the same fact the web UI
        shows. It is written by whichever path found it -- this walk, or a scrape
        that came back signed out -- so a login that breaks mid-day is retried on
        the short clock too, instead of waiting for tomorrow.
        """
        if session_health.read(self.db_path):
            return SUBSCRIPTION_RECONCILE_RETRY_SECONDS
        return SUBSCRIPTION_RECONCILE_INTERVAL_SECONDS

    def _maybe_reconcile_subscriptions(self) -> None:
        """Reconcile the owner's subscriptions onto every target appid, daily.

        Guarded by a monotonic interval for the same reason the staleness sweep
        is: it is a handful of network round trips, and the per-batch path runs
        every few seconds. It runs on the first batch after startup, so a daemon
        that has just come up does not sit on last week's markers.

        The login cookie is refreshed from the browser first. This is the one
        caller that runs on a clock of its own, and the cookie expires on Steam's
        clock, about a day out; without the refresh a daemon that had been up and
        idle would spend its one reconcile of the day on a cookie that died
        overnight. The refresh is a local file copy, and it returns without
        writing anything when the browser's copy has not moved.

        A walk that could not authenticate is retried on a much shorter interval,
        for the reason :meth:`_reconcile_interval` gives.
        """
        now = time.monotonic()
        if (self._last_subscription_reconcile is not None
                and now - self._last_subscription_reconcile
                < self._reconcile_interval()):
            return
        self._last_subscription_reconcile = now
        try:
            self._refresh_login_cookie()
        except Exception as exc:
            # The refresh is the optional half -- the configured cookie may still
            # be good -- so a browser store that cannot be read must not cost the
            # walk. Same rule as every other piece of housekeeping here.
            logging.warning(
                "Login cookie refresh before the subscription reconcile failed: %s", exc)
        self.reconcile_subscriptions()

    def reconcile_subscriptions(self) -> None:
        """Run one reconcile per target appid.

        A failed reconcile is a log line, never an exception: this is
        housekeeping on the fetch loop, and a Steam page that did not load must
        not stop scraping. ``reconcile_own_subscriptions`` itself is also
        non-raising, so the guard here is for anything around it (the appid list,
        a database error) and keeps the contract testable from either side.
        """
        for appid in self.target_appids or []:
            try:
                reconcile_own_subscriptions(self.db_path, appid, self.config,
                                            keep_running=lambda: self.running)
            except Exception as exc:
                logging.warning(
                    "Subscription reconcile for appid %s failed; housekeeping skipped "
                    "this app: %s", appid, exc,
                )

    def _maybe_scan_downloaded_items(self) -> None:
        """Stamp subscribed items Steam has downloaded, at most once a minute.

        Guarded by a monotonic interval for the same reason the staleness sweep
        is: the per-batch path runs every few seconds, and the scan's answer only
        changes when a download finishes. The scan itself is one query plus a
        directory check per unconfirmed subscribed item; it only ever writes, so
        a missing folder, an unplugged drive or a moved library changes nothing.

        A failure is a log line, never an exception: this is housekeeping on the
        fetch loop, and a Steam library that cannot be read must not stop
        scraping. Off Windows (or with no Steam install to read)
        ``WorkshopFolders.scan`` returns without a database access or a log line.
        """
        now = time.monotonic()
        if (self._last_download_scan is not None
                and now - self._last_download_scan < DOWNLOADED_ITEM_SCAN_INTERVAL_SECONDS):
            return
        self._last_download_scan = now
        try:
            self.workshop_folders.scan_downloads()
        except Exception as exc:
            logging.warning(
                "Downloaded-item scan failed; housekeeping skipped this pass: %s", exc)

    def _promote_stale_items(self) -> None:
        """Periodic sweep: promote stale items from API priority 0 to 1.

        The sweep pushes items back into the API queue, so its own rowcount is
        recorded (timestamped) beside the database: the API queue's drain rate is
        net of that inflow, and nothing else records it on our clock. See
        ``src/activity.py``.

        Failures are swallowed deliberately (housekeeping must never stop the
        fetch loop); see notes/findings.md for the capture-on-failure follow-up.
        """
        try:
            threshold = int(time.time()) - self.item_staleness_days * 86400
            conn = get_connection(self.db_path)
            cursor = conn.execute(
                "UPDATE workshop_items SET api_priority = 1 "
                "WHERE api_priority = 0 AND fetch_status = 200 AND api_fetched_at < ? "
                "AND (fetch_status IS NULL OR fetch_status != -1)",
                (threshold,)
            )
            conn.commit()
            promoted = cursor.rowcount
            conn.close()
            if promoted and promoted > 0:
                activity.record_sweep_inflow(self.db_path, promoted)
        except Exception as exc:
            pass
            logging.warning("Stale-item promotion failed; housekeeping skipped this sweep: %s", exc)

    def _acquire_batch(self):
        """Return the next batch of items. Returns None on a database error.

        Refilling is not this method's business: the discovery thread owns it, so
        an empty result here means the queue is genuinely empty rather than
        "discovery has not been run yet".
        """
        # Discovery is the discovery thread's job now. It used to happen here,
        # which meant the fetch loop only refilled the queue after draining it
        # completely: the daemon starved, blocked on paging until enough new
        # items appeared, and then resumed. Batching the details calls made
        # fetching fast enough that the stall became a visible share of the
        # time, so the refill moved off this path entirely.
        return self._read_batch()

    def _read_batch(self, failure_context: str = "Database error in process_batch"):
        """Read one batch from the database. Returns None on database error."""
        try:
            return get_next_items_to_fetch(self.db_path, limit=self.api_batch_size)
        except Exception as e:
            logging.error(f"{failure_context}: {e}")
            time.sleep(5)
            return None

    def _wait_for_work(self) -> None:
        """Wait up to ten minutes for the discovery thread to refill the queue.

        Woken by the thread's signal rather than by polling the database. The
        question "is there work?" is a count over the whole table and nothing
        indexes ``api_priority``, so asking it once a second would cost far more
        than the sleep it replaced; asking a flag costs nothing.

        The flag is checked on each one-second tick rather than by waiting on the
        event, so the tick stays an ordinary ``time.sleep`` -- which is the seam
        the tests stub. Waiting on the event would be marginally quicker to wake
        and would make every test that expects an empty queue sit here for the
        full ten minutes.
        """
        for _ in range(600):
            if not self.running:
                return
            if self._pid_file_removed():
                return
            if self._work_available.is_set():
                self._work_available.clear()
                return
            time.sleep(1)

    def _process_item(self, stored_item: dict, api_data: dict | None = None) -> int | None:
        """Fetch, merge, score, flag and persist a single workshop item.

        ``api_data`` is this item's result from the batch fetch. When it is
        omitted (direct calls, and tests) the item is fetched on its own through
        the single-id spelling; the batch path always supplies it, so the
        fallback never turns one request failure into a request per item.

        Returns the creator id this item would refresh, or None. The refresh
        itself is deferred to the batch so several creators share one request.
        """
        now_ts = int(time.time())
        item_id = stored_item['workshop_id']

        # Step 1: Query API
        if api_data is None:
            api_data = get_workshop_details(item_id, self.api_key)
        api_status = api_data.get("status", 0)

        if api_status not in HANDLED_API_STATUSES:
            # No branch below handles this code, so the item would be persisted as
            # fetch_status 200 and counted as a success. Capture the evidence; changing
            # that flow is a separate decision.
            capture.record_failure(
                kind="api_unhandled_status",
                stage="api_fetch",
                workshop_id=item_id,
                http_status=api_status,
                body=json.dumps(api_data, default=str),
                content_type="application/json",
                context={"handled_statuses": sorted(HANDLED_API_STATUSES)},
            )

        merged_data = stored_item.copy()
        # Attempt clock: set unconditionally, before the status branches, so a
        # 404, a 500 and a success all persist it. This is not optional
        # bookkeeping: get_next_items_to_fetch orders by api_fetched_at ASC,
        # and api_fetched_at now only moves on success, so without the attempt
        # clock a just-failed item keeps its stale api_fetched_at and is retried
        # at the front of its priority band in a tight loop. get_db_stats also
        # reports fetch recency from this column.
        merged_data["last_fetch_attempted_at"] = now_ts
        # The pre-fetch priority is what a temporary failure steps down from, so
        # take it before the queue fields are rewritten below.
        previous_priority = stored_item.get("api_priority") or 0
        merged_data["api_priority"] = 0
        merged_data["fetch_status"] = api_status

        if api_status != 200:
            self._settle_api_failure(merged_data, item_id, api_status, previous_priority)
            return

        # Step 2: Merge, score, and queue follow-up work
        merged_data = self._merge_and_clean_api_data(api_data, merged_data, item_id, now_ts)
        display_title = merged_data.get('title_en') or merged_data.get('title', 'Unknown Title')

        # Capture the pre-fetch priority. The dependent stages filter it down to
        # the part a user asked for -- see user_requested_priority -- so the
        # daemon's own bookkeeping priorities (backlog, retry, discovery) cannot
        # promote an item the enrichment filters excluded above one they selected.
        inherited_priority = stored_item.get("api_priority", 0)

        self._score_wilson(merged_data)
        outcome = self._raise_scrape_and_image_priorities(merged_data, stored_item, item_id, inherited_priority)

        merged_data["fetch_status"] = 200
        insert_or_update_item(self.db_path, merged_data)

        self._queue_translations(merged_data, item_id, outcome.enriched, inherited_priority)

        # Mark what happened, not what the filters decided: the marker answers
        # "was anything queued for this item". Three states:
        #   `current` (grey, SGR 90) -- nothing was queued: the description is
        #       at the item's current revision and the preview needs no attempt;
        #   `enriching` (green, SGR 32) -- the item matched its AppID's
        #       enrichment filters and work was queued for it;
        #   bare -- work was queued, but only as backlog, because the item did
        #       not match the filters.
        # Deciding by `enriched` alone mislabelled the enriched-but-current item
        # as `enriching`, claiming a queue entry that was never made. The old
        # form marked the *rejected* item instead, which was 99.0% of discovery
        # lines (*measured live* 2026-09-17 over the last 6 MB of scraper.log:
        # 53,523 of 54,057), so the one line in a hundred worth reading was the
        # unmarked one. Nothing is lost: an unmarked line is the item that
        # failed the filter and is only scraped as backlog work.
        if outcome.queued and outcome.enriched:
            marker = " — \033[32menriching\033[0m"
        elif outcome.queued:
            marker = ""
        else:
            marker = " — \033[90mcurrent\033[0m"
        logging.info(f"[A:{item_id}] \"{display_title}\"{marker}")
        # Step 3: propose the creator for the batch-level persona refresh. The
        # per-item method no longer makes an HTTP call here; nothing about the
        # delay is touched, because the request already succeeded.
        return self._creator_to_refresh(merged_data, outcome.enriched)

    def _settle_api_failure(self, merged_data: dict, item_id: int, api_status: int,
                            previous_priority: int) -> None:
        """Persist a failed API outcome: dequeue if permanent, step down if not.

        A temporary failure keeps the item queued one priority level lower rather
        than clearing its priority. Clearing it left the item in no queue at all,
        and _promote_stale_items promotes only fetch_status 200, so a transient 500
        became permanent. The floor is 1 because priority 0 means "not queued".

        Statuses with no branch of their own reach here too, on purpose: falling
        through to the success path would persist them as 200 and count them as a
        success. The evidence is captured by the caller before this runs.
        """
        if api_status in PERMANENT_API_STATUSES:
            logging.warning(
                f"[A:{item_id}] Item not found ({api_status}) via API. "
                "Recording the failure and marking it dead (fetch_status=-1)."
            )
            merged_data["fetch_status"] = -1
            merged_data["api_priority"] = 0
            # A dead item can never complete, so clear the other three queue
            # flags as well. The web, image and translation polls select on
            # these columns alone and have no dead-item guard, so a flag left
            # set here keeps the item at the front of a queue that can never
            # drain and spends requests on a page that no longer exists.
            merged_data["needs_web_scrape"] = 0
            merged_data["needs_image"] = 0
            merged_data["translation_priority"] = 0
            insert_or_update_item(self.db_path, merged_data)
            return

        retry_priority = max(1, previous_priority - 1)
        merged_data["api_priority"] = retry_priority
        insert_or_update_item(self.db_path, merged_data)
        logging.error(
            f"[A:{item_id}] API request failed ({api_status}). "
            f"Requeued at priority {retry_priority} to retry after the current queue."
        )
        # Deliberately no `_back_off_api_delay()` here: this is one
        # item's result, not the request's. A batch that returns and parses is a
        # success even when some of its items settle as temporary failures, so
        # only `_fetch_details` moves the delay.

    def _score_wilson(self, merged_data: dict) -> None:
        """Populate the derived Wilson scores from the freshly fetched counts."""
        merged_data["wilson_favorite_score"] = wilson_lower(
            merged_data.get("favorited", 0) or 0,
            merged_data.get("lifetime_subscriptions", 0) or 0)
        merged_data["wilson_subscription_score"] = wilson_lower(
            merged_data.get("subscriptions", 0) or 0,
            merged_data.get("lifetime_subscriptions", 0) or 0)

    def _raise_scrape_and_image_priorities(self, merged_data: dict, stored_item: dict,
                               item_id: int, inherited_priority: int) -> ScrapeImageOutcome:
        """Queue web-scrape and image work for this item.

        Returns a :class:`ScrapeImageOutcome`: whether the item matched its
        AppID's enrichment filters and whether any work was actually queued.
        The two are not the same question -- an enriched item that is already
        current queues nothing -- so the caller must not derive one from the
        other.

        Both stages are gated on the same revision test, because the API refresh
        is the change detector: it is the cheapest call and the only stage that
        goes stale on a timer, so when it observes an unchanged
        steam_updated_at the dependent work is already current and is not
        re-queued. Per-queue staleness sweeps are deliberately not used.

        Note the mixed sources: the revision comparison is between the pre-fetch
        record and the merged one, so both must be passed.

        ``inherited_priority`` is the item's `api_priority` before the fetch. Only the
        part of it a person asked for is carried into these queues -- see
        `user_requested_priority` -- so the item's priority here is
        `max(default_for_this_branch, requested)`.
        """
        # The priority a person asked for, or 0. Applied here rather than by the
        # caller so that every use below shares one answer and a new call site
        # cannot reintroduce the bug this replaced.
        requested_priority = user_requested_priority(inherited_priority)

        old_steam_updated = stored_item.get("steam_updated_at")
        new_steam_updated = merged_data.get("steam_updated_at")
        revision_unchanged = (old_steam_updated is not None
                              and old_steam_updated == new_steam_updated)

        appid = merged_data.get("consumer_appid")
        # The stored description is still the current one, so no scrape can
        # improve it. This is the same test on both branches below: the
        # unenriched path used to queue unconditionally, so every API refresh
        # re-queued a scrape for items whose description was already at the
        # item's revision -- measured live at 120 of the 570 queued scrapes.
        # Worse, that path inherits the item's *api_priority* as the scrape's
        # priority, so merely opening a detail pane (api_priority 10) queued a
        # high-priority scrape that could not change anything.
        description_is_current = (
            revision_unchanged and stored_item.get("extended_description") is not None)

        enriched = False
        queued = False
        # The merge deliberately drops the columns the queue and the folder scan
        # own (MERGE_EXCLUDED_KEYS) so a pre-fetch snapshot cannot clobber them.
        # Those are also the columns a `Subscribed` enrichment filter reads, and
        # a missing key evaluates as NULL rather than raising -- `never`, a false
        # `queued`, a false `downloaded`. Evaluate against the merged record with
        # the pre-fetch values overlaid for exactly those columns, as a copy: the
        # stored record must not carry them back through the merge.
        item_for_filters = dict(merged_data)
        for column in MERGE_EXCLUDED_KEYS:
            item_for_filters.setdefault(column, stored_item.get(column))

        if self._should_enrich(appid, item_for_filters):
            if description_is_current:
                merged_data["extended_description"] = stored_item["extended_description"]
            else:
                raise_web_scrape_priority(self.db_path, item_id, max(3, requested_priority))
                queued = True
            enriched = True
        elif not description_is_current:
            # Does not match the AppID's enrichment filters, so it is not
            # prioritised -- but it is still scraped for anything the page can
            # change, which the test above decides. It sits at backlog priority,
            # which is what this branch always meant: `requested_priority` is zero
            # unless a person asked for the item, so a newly discovered one is
            # queued at 1 rather than carrying its discovery priority (3) into
            # this queue and outranking an item the filters did select.
            raise_web_scrape_priority(self.db_path, item_id, max(1, requested_priority))
            queued = True

        # Image work, on the same revision test. Without it every API fetch
        # re-flagged the image, so previews that had not changed were downloaded
        # again on each staleness cycle. An answer the server has already given
        # -- a 404, a non-image type -- is final whatever the revision says:
        # re-flagging it is exactly how a preview that never existed came to be
        # fetched forever. A real image is still re-fetched when the item is
        # revised, because the preview may have been replaced.
        existing_ext = stored_item.get("image_extension")
        if merged_data.get("preview_url") and not (
                images.blocks_retry(existing_ext)
                or (revision_unchanged and images.can_render_image(existing_ext))):
            raise_image_priority(self.db_path, item_id,
                                 max(3, requested_priority) if enriched else max(1, requested_priority))
            queued = True
        return ScrapeImageOutcome(enriched=enriched, queued=queued)

    def _queue_translations(self, merged_data: dict, item_id: int,
                           enriched: bool, inherited_priority: int) -> None:
        """Flag title and short description for translation.

        The non-ASCII test lives in queue_field_for_translation. What this adds is
        the freshness test: a field whose translation was taken at the item's
        current Steam revision is left alone, so the staleness sweep does not
        re-translate unchanged text, while a field left behind by a source edit
        is re-queued. See translation_is_current.
        """
        if not enriched:
            return
        translation_priority = max(3, user_requested_priority(inherited_priority))
        translate_version = merged_data.get("translate_version")
        steam_updated_at = merged_data.get("steam_updated_at")
        for field, text, translated in [
            ("title_en", merged_data.get("title"), merged_data.get("title_en")),
            ("short_description_en", merged_data.get("short_description"),
             merged_data.get("short_description_en")),
        ]:
            if text and not translation_is_current(translated, translate_version, steam_updated_at):
                queue_field_for_translation(self.db_path, "item", item_id, field, text, translation_priority)

    def _creator_to_refresh(self, merged_data: dict, enriched: bool) -> int | None:
        """Return the creator id this item proposes for a persona refresh.

        The staleness test deliberately lives in `_refresh_creators`, not here:
        it needs a `creators` read, so the batch collects the distinct candidates
        first and asks about each one once.
        """
        creator_id = merged_data.get("creator")
        if not (creator_id and enriched):
            return None
        try:
            return int(creator_id)
        except (ValueError, TypeError):
            # Optional creator-persona enrichment; an unparseable creator value
            # is skipped and the persona is retried on a later cycle.
            return None

    def _refresh_creators(self, creator_ids: list[int]) -> None:
        """Refresh a batch's creator personas in one API call.

        The rules are unchanged from the per-item version -- only enriched items
        propose creators, a user row younger than `creator_staleness_days` is left
        alone, and a creator the API does not return is left for a later cycle --
        but the request count drops from one per item to one per batch.
        """
        if not creator_ids:
            return

        now = int(time.time())
        stale_after = self.creator_staleness_days * 86400
        to_fetch: list[int] = []
        seen: set[int] = set()
        for creator_id in creator_ids:
            if creator_id in seen:
                continue
            seen.add(creator_id)
            try:
                existing_user = get_creator(self.db_path, creator_id)
                if existing_user and existing_user.get("api_fetched_at"):
                    if now - existing_user["api_fetched_at"] < stale_after:
                        continue
            except (ValueError, TypeError):
                # A malformed stored timestamp is skipped; the persona is
                # retried on a later cycle rather than failing the batch.
                continue
            to_fetch.append(creator_id)

        if not to_fetch:
            return

        summaries = get_player_summaries(to_fetch, self.api_key)
        for creator_id in to_fetch:
            if creator_id in summaries:
                self._store_user_record(creator_id, summaries[creator_id].get("personaname"))

    def _back_off_api_delay(self) -> None:
        """Multiply the delay once for a refused request and clear the streak.

        Called once per POST from `_fetch_details`, never from the per-item
        failure path. A request that returns and parses is a success however its
        individual results read -- 50 details of which 10 are "not found" is a
        completely successful API call -- so per-item outcomes must not reach
        here.

        Every refusal multiplies (rather than waiting for a second strike), so a
        sustained outage backs off geometrically and the delay can keep climbing
        until it is under the limit. The multiplication is capped only by the
        ceiling; see the module comment.
        """
        self.api_failures += 1
        self.api_successes = 0
        self._api_clock.since()
        old_delay = self.api_delay
        self.api_delay = pacing.backoff(self.api_delay)
        if old_delay != self.api_delay:
            set_api_delay(self.api_delay)
            logging.info(
                f"{self.api_failures} consecutive failed API requests! "
                f"Increasing API delay from {old_delay} to {self.api_delay}s.")
            # Persist immediately: a restart during an outage must not resume at
            # the old, refused pace.
            self._persisted_api_delay = self.api_delay
            self._save_config_value(
                "api_delay_seconds", pacing.persistable(self.api_delay))

    def _decay_api_delay(self) -> None:
        """Shave one step off the delay for a healthy request.

        A success is a request that returned and parsed; the individual results
        are item state, not pacing. Every success decays the delay rather than
        waiting for a long streak, which is what makes the client keep probing
        for a higher sustainable rate and converge on the limit. The decay is
        persisted only once it has moved far enough to be worth a config write.
        """
        self.api_successes += 1
        self.api_failures = 0
        old_delay = self.api_delay
        self.api_delay = pacing.decay(
            self.api_delay, self._api_clock.since(), API_DELAY_FLOOR)
        if old_delay != self.api_delay:
            set_api_delay(self.api_delay)
            if pacing.needs_persist(self.api_delay, self._persisted_api_delay):
                self._persisted_api_delay = self.api_delay
                logging.info(
                    f"Healthy API requests; decreasing API delay to {self.api_delay} seconds.")
                self._save_config_value(
                    "api_delay_seconds", pacing.persistable(self.api_delay))
            else:
                # One line per step would flood the log while the delay walks
                # down from a back-off; the persisted steps are the reportable
                # ones.
                logging.debug(
                    "API request succeeded; decreasing API delay from %s to %s seconds.",
                    old_delay, self.api_delay)

    def _pid_file_removed(self) -> bool:
        if not self.running:
            return False
        exists = os.path.exists(".daemon.pid")
        if exists and not self._saw_pid_file:
            self._saw_pid_file = True
        if self._saw_pid_file and not exists:
            logging.info("PID file removed — initiating graceful shutdown")
            self.running = False
            return True
        return False

    def run(self):
        """Main loop that continuously queries and scrapes."""
        logging.info("Starting daemon loop...")
        self.translator.start()
        self._discovery_thread = DiscoveryThread(self)
        self._discovery_thread.start()
        self._web_worker = WebScraperThread(self.db_path, self.pause_lock_file, daemon_config=self.config.get("daemon", {}), save_callback=self._save_config_value,
                                              session_refresh=self._refresh_login_cookie)
        self._web_worker.start()
        self._image_worker = ImageDownloadThread(self.db_path, self.pause_lock_file, daemon_config=self.config.get("daemon", {}), save_callback=self._save_config_value)
        self._image_worker.start()
        if self._backup_worker is not None:
            self._backup_worker.start()
        while self.running:
            self.process_batch()
            self._pid_file_removed()
        logging.info("Daemon gracefully exited.")
        if self._discovery_thread is not None:
            self._discovery_thread.stop()
            self._discovery_thread.join(timeout=5)
            logging.info("Discovery thread stopped.")
        self._web_worker.running = False
        self._web_worker.join(timeout=5)
        logging.info("Web scraper thread stopped.")
        self._image_worker.running = False
        self._image_worker.join(timeout=5)
        logging.info("Image download thread stopped.")
        self.translator.running = False
        self.translator.join(timeout=5)
        logging.info("Translator thread stopped.")
        if self._backup_worker is not None:
            self._backup_worker.running = False
            self._backup_worker.join(timeout=5)
            logging.info("Backup thread stopped.")
            # Final synchronous snapshot, taken only after every writer thread
            # has been joined so no writer can be mid-transaction. Backup
            # failures are logged and swallowed: shutdown must still complete.
            try:
                self._backup_worker.snapshot_now()
            except Exception as e:
                logging.error(f"Final database backup failed: {e}")

        # Counters are flushed on a timer during the run; this catches whatever
        # was recorded since the last flush. Failures are logged, not raised:
        # shutdown must still complete.
        try:
            capture.flush()
        except Exception as e:
            logging.error(f"Final failure-capture flush failed: {e}")

    def seed_database(self, fill_target: int = DISCOVERY_FILL_TARGET):
        """
        Discovers workshop items via IPublishedFileService/QueryFiles API
        using cursor-based pagination (unlimited depth).
        """
        if not self.api_key:
            logging.error("No Steam API key configured. Discovery cannot run. "
                          "Set STEAM_API_KEY environment variable or add api.key to config.yaml.")
            return 0

        discovered_total = 0
        for appid in self.target_appids:
            # The guard must measure work the fetch queue can actually hand out.
            # It used to test count_never_fetched_items -- items never successfully
            # fetched -- which is a disjoint population: on production this read
            # 890 while the fetch queue held 1, so discovery was suppressed
            # permanently and the queue could never refill.
            fetchable = count_fetchable_items(self.db_path)
            if fetchable >= fill_target:
                logging.info(
                    "Queue appropriately filled (%d fetchable, >= %d) for AppID %s. "
                    "Skipping discovery. (%d items have never been fetched but are not queued.)",
                    fetchable, fill_target, appid, count_never_fetched_items(self.db_path),
                )
                continue

            app_tracking = get_app_tracking(self.db_path, appid)
            cursor = (app_tracking or {}).get("last_cursor") or "*"
            new_discovered_count = 0
            pages = 0

            logging.info(f"Discovering items for AppID {appid}, resuming from cursor...")
            while cursor and self.running:
                if self._pid_file_removed():
                    break
                result = query_workshop_newest_page(appid, cursor=cursor, api_key=self.api_key,
                                                    keep_running=self._discovery_alive)
                if result.get("abandoned"):
                    logging.info("Abandoned discovery for AppID %s: the daemon is stopping.", appid)
                    break
                if result.get("failed"):
                    # Discovery requests are requests on the same key and the same
                    # budget, so a refusal here is evidence about the rate exactly
                    # as a refused details call is. Not recording it left the
                    # controller blind to half its traffic -- tolerable while it
                    # was serialised behind the fetch loop, not once it runs on
                    # its own thread.
                    self._back_off_api_delay()
                    logging.error(f"API error for AppID {appid}. Halting discovery.")
                    break
                self._decay_api_delay()

                if pages == 0 and result["total"]:
                    logging.info(f"AppID {appid} has ~{result['total']} total items.")

                page_new_count = 0
                for item in result["items"]:
                    wid = int(item.get("publishedfileid", 0))
                    # Pass api_priority explicitly rather than leaning on the column
                    # default: CREATE TABLE declares DEFAULT 3 but the migration that
                    # adds the column to an older database uses DEFAULT 0, so the same
                    # insert queues the item on a fresh database and strands it on a
                    # migrated one (the fetch queue selects api_priority > 0). 3 is the
                    # documented new-item priority (data-model.md, and migration 11->12
                    # sets never-scraped rows to 3), so this also leaves the fresh
                    # database's queue order unchanged.
                    if wid and insert_or_update_item(self.db_path, {"workshop_id": wid, "api_priority": 3}):
                        page_new_count += 1

                new_discovered_count += page_new_count
                pages += 1
                logging.info(f"Cursor page {pages} provided {page_new_count} new items. (Total new: {new_discovered_count})")

                cursor = result.get("next_cursor") or ""
                if cursor:
                    update_app_tracking_cursor(self.db_path, appid, cursor)
                pacing.wait(self.api_delay, lambda: self.running)

                if new_discovered_count >= fill_target:
                    logging.info(f"Discovered {new_discovered_count} new items for AppID {appid}, enough for now.")
                    break

            if pages:
                logging.info(f"Finished scanning {pages} pages for AppID {appid}. Discovered {new_discovered_count} new items.")
                if not cursor:
                    self._cursor_exhausted = True
                    logging.info("Cursor exhausted — page-based discovery now eligible.")

            discovered_total += new_discovered_count

        return discovered_total

    def _page_discovery_eligible(self) -> bool:
        if os.path.exists('.fetch_new'):
            return True
        if self._cursor_exhausted:
            return True
        conn = get_connection(self.db_path)
        scraped = conn.execute(
            "SELECT COUNT(*) FROM workshop_items WHERE api_fetched_at IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        return scraped >= 500

    def _run_page_discovery(self):
        if not self.api_key:
            return
        if int(time.time()) - self._last_page_discovery < 86400:
            if not os.path.exists('.fetch_new'):
                return
            logging.info("Fetch-new trigger file detected — bypassing 24h cooldown")
            try:
                os.remove('.fetch_new')
            except OSError as exc:
                pass
                logging.warning(
                    "Could not remove .fetch_new (%s); page discovery will keep bypassing the 24h cooldown.",
                    exc,
                )

        logging.info("Running page-based discovery (sort-by-update-time)...")
        self._last_page_discovery = int(time.time())

        for appid in self.target_appids:
            if not self.running:
                break
            cursor = "*"
            page = 0
            while cursor and self.running and page < 500:
                result = query_workshop_updated_page(
                    appid, cursor, self.api_key, keep_running=self._discovery_alive)
                if result.get("abandoned"):
                    logging.info("Abandoned page discovery for AppID %s: the daemon is stopping.", appid)
                    break
                if result.get("failed"):
                    self._back_off_api_delay()
                    logging.error(f"Page discovery error for AppID {appid}.")
                    break
                self._decay_api_delay()

                items = result.get("items", [])
                if page == 0:
                    logging.info(f"Page mode for AppID {appid}: ~{result.get('total', '?')} total items by update time.")

                page_new = 0
                wid_to_api_updated = {int(it.get("publishedfileid", 0)): it.get("time_updated") or 0 for it in items if it.get("publishedfileid")}

                # Fetch existing steam_updated_at in one query
                conn = get_connection(self.db_path)
                placeholders = ",".join("?" * len(wid_to_api_updated))
                existing_rows = {}
                if wid_to_api_updated:
                    rows = conn.execute(
                        f"SELECT workshop_id, steam_updated_at FROM workshop_items WHERE workshop_id IN ({placeholders})",
                        list(wid_to_api_updated.keys()),
                    ).fetchall()
                    existing_rows = {r["workshop_id"]: r["steam_updated_at"] for r in rows}

                for wid, api_updated in wid_to_api_updated.items():
                    db_updated = existing_rows.get(wid)
                    if db_updated is None:
                        # Wholly new item
                        page_new += 1
                    elif api_updated and (not db_updated or api_updated > db_updated):
                        # Existing item with a fresher update time on Steam
                        page_new += 1

                    insert_or_update_item(self.db_path, {"workshop_id": wid, "api_priority": 5})

                conn.close()
                page += 1

                if page_new == 0:
                    logging.info(f"Page mode: no new or updated items on page {page}, stopping for AppID {appid}.")
                    break

                logging.info(f"Page mode: page {page} added/updated {page_new} items for AppID {appid}.")
                cursor = result.get("next_cursor") or ""
                pacing.wait(self.api_delay, lambda: self.running)

        logging.info("Page-based discovery complete.")
