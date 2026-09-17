import time
import math
import signal
import json
import logging
import os
import threading
from datetime import datetime, timezone
from src.database import (
    get_next_items_to_scrape, 
    insert_or_update_item, 
    count_unscraped_items, 
    count_fetchable_items, 
    insert_or_update_user, 
    get_user, 
    flag_for_translation,
    get_app_tracking,
    update_app_tracking_cursor,
    save_app_filter,
    get_connection,
    get_item_details,
    normalize_tags,
    _evaluate_filters,
    WORKSHOP_ITEM_COLUMNS,
)
from src.steam_api import (
    get_workshop_details_api,
    get_workshop_details_batch,
    query_workshop_items,
    get_player_summaries,
    query_workshop_files,
    set_api_delay,
    query_workshop_page_updated,
    STEAM_API_MAX_IDS_PER_REQUEST,
)
from src.translator import TranslatorThread, is_ascii
from src.config import login_secure_value, save_config
from src.database import flag_for_web_scrape, flag_field_for_translation, flag_for_image, translation_is_current
from src.firefox_cookies import steam_login_secure
from src.web_worker import WebScraperThread
from src.image_worker import ImageScraperThread
from src.backup import BackupThread
from src.daemon_state import StateStore, state_path_for
from src import pacing
from src import images
from src import capture
from src import session_health
from src.subscription_sync import reconcile_own_subscriptions

# API statuses the fetch path has an explicit branch for. Anything else is
# captured as evidence and then treated as temporary by _settle_api_failure; it
# is never silently persisted as a success.
HANDLED_API_STATUSES = frozenset({200, 404, 500})

# The only API outcome that cannot succeed on retry. Everything else -- 500,
# transport exceptions (which get_workshop_details_api reports as 500), and any
# status without its own branch -- is retried at one priority level lower.
PERMANENT_API_STATUSES = frozenset({404})


# --- API request backoff -----------------------------------------------------
# The delay is a property of the *request*, not of the items it carried. One
# batched GetPublishedFileDetails call returns up to
# STEAM_API_MAX_IDS_PER_REQUEST results, so per-item signals are the wrong unit:
# a single overloaded call would be diluted by the results that were fine, and a
# batch of "not found" results -- a perfectly successful call -- would read as a
# run of failures. Only the request outcome moves the delay; per-item results
# drive item state (status, priority, queue flags, death) and nothing else.
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


# --- API merge allow-list ----------------------------------------------------
# Item columns that are owned by the queue-flagging helpers rather than by the
# API merge. They must NOT survive a merge: flag_for_web_scrape / flag_for_image
# set them explicitly between the merge and the insert, so carrying a stale value
# through the merge would clobber the flag that was just set.
MERGE_EXCLUDED_KEYS = frozenset({
    "is_queued_for_subscription",
    "needs_web_scrape",
    "image_extension",
    "needs_image",
})

# Keys retained from an API merge into the item record:
#   * every real column (WORKSHOP_ITEM_COLUMNS), minus the queue-owned ones above
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

    def __init__(self, owner: "Daemon", interval: float = DISCOVERY_IDLE_SECONDS):
        super().__init__(daemon=True)
        self.owner = owner
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
        self.last_filters = {}
        
        # Implement default fallbacks
        self.db_path = config.get("database", {}).get("path", "workshop.db")
        self.api_key = config.get("api", {}).get("key", "")
        self.batch_size = config.get("daemon", {}).get("batch_size", 10)
        daemon_config = config.get("daemon", {})
        if daemon_config.get("api_delay_seconds") is None and daemon_config.get("request_delay_seconds") is not None:
            logging.warning(
                "Config key 'request_delay_seconds' is deprecated and still honoured; "
                "rename it to 'api_delay_seconds'."
            )
        self.api_delay = daemon_config.get("api_delay_seconds") or daemon_config.get("request_delay_seconds", 1.5)
        self.item_staleness_days = int(daemon_config.get("item_staleness_days") or 30)
        self.user_staleness_days = int(daemon_config.get("user_staleness_days") or 90)
        set_api_delay(self.api_delay)
        logging.info(f"API delay={self.api_delay}s, Staleness: item={self.item_staleness_days}d, user={self.user_staleness_days}d")

        # Write default config keys if absent
        changed = False
        if "daemon" not in self.config:
            self.config["daemon"] = {}
        for key, val in [("item_staleness_days", self.item_staleness_days),
                          ("user_staleness_days", self.user_staleness_days)]:
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
        # image switch is separate because the web switch keeps whole page
        # bodies unbounded, and an owner reviewing image metadata should not have
        # to collect pages to do it. Image *failures* need neither switch — the
        # outbox alone is enough, like every other failure capture.
        self.capture_web_scrapes = bool(daemon_config.get("capture_web_scrapes", False))
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
        capture.configure(self.outbox_dir, self.capture_web_scrapes,
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
        
        # Pre-load initial filter state to avoid false positives on startup
        self._load_initial_filter_state()
        
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
        save_config(self.config_path, self.config)
        logging.info("Login cookie refreshed from the browser and saved to the config.")
        return True

    def _build_user_record(self, steamid: int, personaname: str) -> dict:
        """Builds a user record dict for upsert, flagging for translation if non-ASCII."""
        record = {
            "steamid": steamid,
            "personaname": personaname,
            "api_fetched_at": int(time.time())
        }
        if not is_ascii(personaname):
            record["translation_priority"] = 1
        return record

    def _merge_and_clean_api_data(self, api_data: dict, existing_data: dict, item_id: int, now_ts: int) -> dict:
        """Merges API response into existing data, remaps column names, and filters to allowed keys."""
        merged = existing_data.copy()
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
                val_preview = str(v)[:20] + "..." if len(str(v)) > 20 else str(v)
                logger = logging.info if v is not None and str(v).strip() != "" else logging.debug
                logger(f"Discarding unknown API column: '{k}' with value '{val_preview}' for item {item_id}")

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
        enrichment = app_tracking.get("enrichment_filters") or "[]"
        try:
            filters = json.loads(enrichment)
        except (json.JSONDecodeError, TypeError):
            return True
        if not filters:
            # Fallback to legacy columns for backward compat
            filter_text = (app_tracking.get("filter_text") or "").strip()
            required_tags = json.loads(app_tracking.get("required_tags") or "[]")
            excluded_tags = json.loads(app_tracking.get("excluded_tags") or "[]")
            if not filter_text and not required_tags and not excluded_tags:
                return True
            # Convert legacy columns to filter format for evaluation
            filters = []
            if filter_text:
                filters.append({"field": "Title", "op": "contains", "value": filter_text})
            for tag in required_tags:
                filters.append({"field": "Tags", "op": "contains", "value": tag})
            for tag in excluded_tags:
                filters.append({"field": "Tags", "op": "does_not_contain", "value": tag})
        return _evaluate_filters(item, filters)

    def _load_initial_filter_state(self):
        """Pre-loads filter state and page tracking from the DB on startup."""
        for appid in self.target_appids:
            app_tracking = get_app_tracking(self.db_path, appid)
            if app_tracking:
                self.last_filters[appid] = {
                    "last_page": app_tracking.get("last_page_scanned", 0) or 0,
                    "last_cursor": app_tracking.get("last_cursor") or ""
                }

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

    def expand_user_discovery(self):
        """
        Scans workshop_items for creators who are not in the users table 
        and fetches their summaries.
        """
        conn = get_connection(self.db_path)
        # Find creators in workshop_items that aren't in users table
        sql = """
            SELECT DISTINCT creator FROM workshop_items 
            WHERE creator IS NOT NULL 
            AND creator NOT IN (SELECT steamid FROM users)
            LIMIT 100
        """
        cursor = conn.execute(sql)
        missing_ids = [int(row["creator"]) for row in cursor.fetchall() if row["creator"]]
        conn.close()

        if missing_ids:
            logging.info(f"Proactively fetching {len(missing_ids)} missing user profiles...")
            try:
                summaries = get_player_summaries(missing_ids, self.api_key)
                for sid in missing_ids:
                    if sid in summaries:
                        pdata = summaries[sid]
                        user_record = self._build_user_record(sid, pdata.get("personaname"))
                        insert_or_update_user(self.db_path, user_record)
                        logging.info(f"Updated profile for user {sid}: '{user_record['personaname']}'")
                    else:
                        insert_or_update_user(self.db_path, self._build_user_record(sid, f"SteamID:{sid}"))
            except Exception as e:
                logging.error(f"Error expanding user discovery: {e}")

    def process_batch(self):
        """Process one batch: housekeeping, acquire work, then process each item."""
        self._maybe_promote_stale_items()
        self._maybe_reconcile_subscriptions()

        items_to_scrape = self._acquire_batch()
        if items_to_scrape is None:
            return  # database error, already logged
        if not items_to_scrape:
            self._wait_for_work()
            return

        if not self.running or self._pid_file_removed():
            return

        # One bulk details request for the whole batch (chunked only if the
        # configured batch_size exceeds the endpoint ceiling). Each request's
        # outcome drives the backoff; the per-item results below only decide
        # each item's state.
        api_data_by_id = self._fetch_details(items_to_scrape)

        creators_to_refresh = []
        for existing_data in items_to_scrape:
            if not self.running or self._pid_file_removed():
                break
            # Items are still processed in the order the queue returned them;
            # each is matched to its own result by id.
            creator_id = self._process_item(
                existing_data,
                api_data=api_data_by_id.get(existing_data["workshop_id"]),
            )
            if creator_id is not None:
                creators_to_refresh.append(creator_id)

        # Creator personas move from one request per item to one per batch.
        self._refresh_creators(creators_to_refresh)

    def _fetch_details(self, items: list[dict]) -> dict[int, dict]:
        """Fetch details for a batch in as few requests as the API allows.

        This is the only place request-level outcomes are counted: one call to
        `_record_api_request_failure` or `_record_api_request_success` per POST.
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
                self._record_api_request_failure()
                for item_id in ids:
                    api_data_by_id[item_id] = {"status": 500, "publishedfileid": item_id}
            else:
                self._record_api_request_success()
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
                reconcile_own_subscriptions(self.db_path, appid, self.config)
            except Exception as exc:
                logging.warning(
                    "Subscription reconcile for appid %s failed; housekeeping skipped "
                    "this app: %s", appid, exc,
                )

    def _promote_stale_items(self) -> None:
        """Periodic sweep: promote stale items from API priority 0 to 1.

        Failures are swallowed deliberately (housekeeping must never stop the
        fetch loop); see notes/findings.md for the capture-on-failure follow-up.
        """
        try:
            threshold = int(time.time()) - self.item_staleness_days * 86400
            conn = get_connection(self.db_path)
            conn.execute(
                "UPDATE workshop_items SET api_priority = 1 "
                "WHERE api_priority = 0 AND status = 200 AND api_fetched_at < ? "
                "AND (status IS NULL OR status != -1)",
                (threshold,)
            )
            conn.commit()
            conn.close()
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
        return self._fetch_batch()

    def _fetch_batch(self, error_message: str = "Database error in process_batch"):
        """Read one batch from the database. Returns None on database error."""
        try:
            return get_next_items_to_scrape(self.db_path, limit=self.batch_size,
                                            staleness_days=self.item_staleness_days)
        except Exception as e:
            logging.error(f"{error_message}: {e}")
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

    def _process_item(self, existing_data: dict, api_data: dict | None = None) -> int | None:
        """Fetch, merge, score, flag and persist a single workshop item.

        ``api_data`` is this item's result from the batch fetch. When it is
        omitted (direct calls, and tests) the item is fetched on its own through
        the single-id spelling; the batch path always supplies it, so the
        fallback never turns one request failure into a request per item.

        Returns the creator id this item would refresh, or None. The refresh
        itself is deferred to the batch so several creators share one request.
        """
        now_ts = int(time.time())
        item_id = existing_data['workshop_id']

        # Step 1: Query API
        if api_data is None:
            api_data = get_workshop_details_api(item_id, self.api_key)
        api_status = api_data.get("status", 0)

        if api_status not in HANDLED_API_STATUSES:
            # No branch below handles this code, so the item would be persisted as
            # status 200 and counted as a success. Capture the evidence; changing
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

        merged_data = existing_data.copy()
        # Attempt clock: set unconditionally, before the status branches, so a
        # 404, a 500 and a success all persist it. This is not optional
        # bookkeeping: get_next_items_to_scrape orders by api_fetched_at ASC,
        # and api_fetched_at now only moves on success, so without the attempt
        # clock a just-failed item keeps its stale api_fetched_at and is retried
        # at the front of its priority band in a tight loop. get_db_stats also
        # reports fetch recency from this column.
        merged_data["last_fetch_attempted_at"] = now_ts
        # The pre-fetch priority is what a temporary failure steps down from, so
        # take it before the queue fields are rewritten below.
        previous_priority = existing_data.get("api_priority") or 0
        merged_data["api_priority"] = 0
        merged_data["status"] = api_status

        if api_status != 200:
            self._settle_api_failure(merged_data, item_id, api_status, previous_priority)
            return

        # Step 2: Merge, score, and queue follow-up work
        merged_data = self._merge_and_clean_api_data(api_data, merged_data, item_id, now_ts)
        display_title = merged_data.get('title_en') or merged_data.get('title', 'Unknown Title')

        # Capture the pre-fetch priority to inherit for image/web/translation flagging
        inherited_prio = existing_data.get("api_priority", 0)

        self._score_wilson(merged_data)
        enriched = self._flag_scrape_and_image(merged_data, existing_data, item_id, inherited_prio)

        merged_data["status"] = 200
        insert_or_update_item(self.db_path, merged_data)

        self._flag_translations(merged_data, item_id, enriched, inherited_prio)

        logging.info(f"[A:{item_id}] \"{display_title}\"{' — \033[31mignored\033[0m' if not enriched else ''}")
        # Step 3: propose the creator for the batch-level persona refresh. The
        # per-item method no longer makes an HTTP call here; nothing about the
        # delay is touched, because the request already succeeded.
        return self._creator_to_refresh(merged_data, enriched)

    def _settle_api_failure(self, merged_data: dict, item_id: int, api_status: int,
                            previous_priority: int) -> None:
        """Persist a failed API outcome: dequeue if permanent, step down if not.

        A temporary failure keeps the item queued one priority level lower rather
        than clearing its priority. Clearing it left the item in no queue at all,
        and _promote_stale_items promotes only status 200, so a transient 500
        became permanent. The floor is 1 because priority 0 means "not queued".

        Statuses with no branch of their own reach here too, on purpose: falling
        through to the success path would persist them as 200 and count them as a
        success. The evidence is captured by the caller before this runs.
        """
        if api_status in PERMANENT_API_STATUSES:
            logging.warning(
                f"[A:{item_id}] Item not found ({api_status}) via API. "
                "Recording the failure and marking it dead (status=-1)."
            )
            merged_data["status"] = -1
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
        # Deliberately no `_record_api_request_failure()` here: this is one
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

    def _flag_scrape_and_image(self, merged_data: dict, existing_data: dict,
                               item_id: int, inherited_prio: int) -> bool:
        """Queue web-scrape and image work for this item.

        Returns whether the item was enriched. Both stages are gated on the same
        revision test, because the API refresh is the change detector: it is the
        cheapest call and the only stage that goes stale on a timer, so when it
        observes an unchanged steam_updated_at the dependent work is already
        current and is not re-queued. Per-queue staleness sweeps are deliberately
        not used.

        Note the mixed sources: the revision comparison is between the pre-fetch
        record and the merged one, so both must be passed.
        """
        old_steam_updated = existing_data.get("steam_updated_at")
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
            revision_unchanged and existing_data.get("extended_description") is not None)

        enriched = False
        if self._should_enrich(appid, merged_data):
            if description_is_current:
                merged_data["extended_description"] = existing_data["extended_description"]
            else:
                flag_for_web_scrape(self.db_path, item_id, max(3, inherited_prio))
            enriched = True
        elif not description_is_current:
            # Does not match the AppID's enrichment filters, so it is not
            # prioritised -- but it is still scraped for anything the page can
            # change, which the test above decides.
            flag_for_web_scrape(self.db_path, item_id, max(1, inherited_prio))

        # Image work, on the same revision test. Without it every API fetch
        # re-flagged the image, so previews that had not changed were downloaded
        # again on each staleness cycle. An answer the server has already given
        # -- a 404, a non-image type -- is final whatever the revision says:
        # re-flagging it is exactly how a preview that never existed came to be
        # fetched forever. A real image is still re-fetched when the item is
        # revised, because the preview may have been replaced.
        existing_ext = existing_data.get("image_extension")
        if merged_data.get("preview_url") and not (
                images.blocks_retry(existing_ext)
                or (revision_unchanged and images.can_render_image(existing_ext))):
            flag_for_image(self.db_path, item_id,
                           max(3, inherited_prio) if enriched else max(1, inherited_prio))
        return enriched

    def _flag_translations(self, merged_data: dict, item_id: int,
                           enriched: bool, inherited_prio: int) -> None:
        """Flag title and short description for translation.

        The non-ASCII test lives in flag_field_for_translation. What this adds is
        the freshness test: a field whose translation was taken at the item's
        current Steam revision is left alone, so the staleness sweep does not
        re-translate unchanged text, while a field left behind by a source edit
        is re-queued. See translation_is_current.
        """
        if not enriched:
            return
        t_prio = max(3, inherited_prio)
        version = merged_data.get("translate_version")
        steam_updated = merged_data.get("steam_updated_at")
        for field, text, translated in [
            ("title_en", merged_data.get("title"), merged_data.get("title_en")),
            ("short_description_en", merged_data.get("short_description"),
             merged_data.get("short_description_en")),
        ]:
            if text and not translation_is_current(translated, version, steam_updated):
                flag_field_for_translation(self.db_path, "item", item_id, field, text, t_prio)

    def _creator_to_refresh(self, merged_data: dict, enriched: bool) -> int | None:
        """Return the creator id this item proposes for a persona refresh.

        The staleness test deliberately lives in `_refresh_creators`, not here:
        it needs a `users` read, so the batch collects the distinct candidates
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
        propose creators, a user row younger than `user_staleness_days` is left
        alone, and a creator the API does not return is left for a later cycle --
        but the request count drops from one per item to one per batch.
        """
        if not creator_ids:
            return

        now = int(time.time())
        stale_after = self.user_staleness_days * 86400
        to_fetch: list[int] = []
        seen: set[int] = set()
        for creator_id in creator_ids:
            if creator_id in seen:
                continue
            seen.add(creator_id)
            try:
                existing_user = get_user(self.db_path, creator_id)
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
                insert_or_update_user(self.db_path, self._build_user_record(
                    creator_id, summaries[creator_id].get("personaname")))

    def _record_api_request_failure(self) -> None:
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

    def _record_api_request_success(self) -> None:
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
        self._image_worker = ImageScraperThread(self.db_path, self.pause_lock_file, daemon_config=self.config.get("daemon", {}), save_callback=self._save_config_value)
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
                self._backup_worker.run_now()
            except Exception as e:
                logging.error(f"Final database backup failed: {e}")

        # Counters are flushed on a timer during the run; this catches whatever
        # was recorded since the last flush. Failures are logged, not raised:
        # shutdown must still complete.
        try:
            capture.flush()
        except Exception as e:
            logging.error(f"Final failure-capture flush failed: {e}")

    def seed_database(self, target_new: int = 100):
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
            # It used to test count_unscraped_items -- items never successfully
            # fetched -- which is a disjoint population: on production this read
            # 890 while the fetch queue held 1, so discovery was suppressed
            # permanently and the queue could never refill.
            fetchable = count_fetchable_items(self.db_path)
            if fetchable >= target_new:
                logging.info(
                    "Queue appropriately filled (%d fetchable, >= %d) for AppID %s. "
                    "Skipping discovery. (%d items have never been fetched but are not queued.)",
                    fetchable, target_new, appid, count_unscraped_items(self.db_path),
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
                result = query_workshop_files(appid, cursor=cursor, api_key=self.api_key,
                                              keep_running=self._discovery_alive)
                if result.get("abandoned"):
                    logging.info("Abandoned discovery for AppID %s: the daemon is stopping.", appid)
                    break
                if result.get("error"):
                    # Discovery requests are requests on the same key and the same
                    # budget, so a refusal here is evidence about the rate exactly
                    # as a refused details call is. Not recording it left the
                    # controller blind to half its traffic -- tolerable while it
                    # was serialised behind the fetch loop, not once it runs on
                    # its own thread.
                    self._record_api_request_failure()
                    logging.error(f"API error for AppID {appid}. Halting discovery.")
                    break
                self._record_api_request_success()

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

                if new_discovered_count >= target_new:
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
                result = query_workshop_page_updated(
                    appid, cursor, self.api_key, keep_running=self._discovery_alive)
                if result.get("abandoned"):
                    logging.info("Abandoned page discovery for AppID %s: the daemon is stopping.", appid)
                    break
                if result.get("error"):
                    self._record_api_request_failure()
                    logging.error(f"Page discovery error for AppID {appid}.")
                    break
                self._record_api_request_success()

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
