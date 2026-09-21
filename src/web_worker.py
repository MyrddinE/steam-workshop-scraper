"""Background thread for web scraping Steam Workshop pages."""

import enum
import time
import os
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from src.database import get_next_web_scrape_item, insert_or_update_item, get_connection, queue_field_for_translation, translation_is_current
from src import pacing
from src import session_health
from src.daemon_state import StateStore, state_path_for
from src.config import warn_retired_key
from src.web_scraper import (DESCRIPTION_SELECTOR, ITEM_MISSING_HTTP_STATUSES, looks_like_gated,
                             looks_like_item_page_without_description, looks_like_missing_item,
                             looks_like_rate_limited, looks_like_signed_out, missing_item_reason,
                             scrape_extended_details)
from src import capture

# Steam's per-account request budget refills over minutes, so the useful
# response to its throttle page is a pause measured in minutes, not the
# seconds used for ordinary pacing.

# The slowest the decay rule will take the scraper. The owner learned the figure
# empirically: the same Steam budget is shared with their own hand-browsing, so
# when scrapes start failing the worker has to back off far enough that the
# Workshop is still usable by hand while the daemon runs. The old 1.0 s floor
# left no room for that.
WEB_DELAY_FLOOR = 6.0

# The starting delay. Set to the floor rather than below it, so an installation
# that has never written a delay starts at the slowest rate the decay will take
# it to anyway.
WEB_DELAY_DEFAULT = WEB_DELAY_FLOOR


def web_delay_store_for(config: dict) -> StateStore | None:
    """The daemon state store the shared web delay lives in, for ``config``.

    The delay is state, not configuration, so it is stored beside the database
    the config points at. A config with no ``database.path`` falls back to the
    same default the rest of the app uses (``workshop.db`` in the working
    directory), so the resolver never fails on a partial config.
    """
    if not isinstance(config, dict):
        return None
    database = config.get("database")
    db_path = database.get("path") if isinstance(database, dict) else None
    return StateStore(state_path_for(db_path or "workshop.db"))


def configured_web_delay(config: dict) -> float:
    """The shared web interval currently persisted, floored at the floor.

    The delay is adaptive and it is daemon **state**, not configuration: it is
    written to the daemon state file beside the database (one section per
    worker, ``src/daemon_state.py``) and read back from there. The TUI and the
    web server run in different processes from the daemon's worker, and the
    state file -- not an in-memory config dict -- is what they can all see, so
    this reads it *fresh* on every call rather than caching it.

    A config that still carries the retired ``daemon.web_delay_seconds`` key is
    ignored; the warning is logged once per process by
    :func:`src.config.warn_retired_key`. With no persisted value the default
    applies, and the result is floored, because a delay under the floor is one
    the decay would not have chosen.
    """
    daemon = config.get("daemon") if isinstance(config, dict) else None
    if isinstance(daemon, dict) and "web_delay_seconds" in daemon:
        warn_retired_key("daemon", "web_delay_seconds")
    stored = pacing.load_delay(web_delay_store_for(config), pacing.WEB_DELAY_SECTION)
    if stored is None:
        return WEB_DELAY_DEFAULT
    return max(WEB_DELAY_FLOOR, stored)


class ScrapeOutcome(enum.Enum):
    """What one web scrape attempt turned out to be, and so what it earns.

    The policy is a taxonomy rather than a chain of conditions so that the next
    person can see, in one place, which outcome buys which response. The run
    loop switches on this; ``classify_scrape`` is the only place a result is
    read.

    Pacing is deliberately not uniform. A slower pace can only help when the
    request itself was the problem -- the server refused to serve or could not
    be reached -- so only ``RATE_LIMITED`` (with its own long pause) and
    ``UNKNOWN`` move the delay. An answer the item gave, whether "no such item"
    or "no description", and a session that is not working cannot be fixed by
    knocking more slowly.
    """

    # The description selector matched: a completed scrape.
    SUCCESS = "success"
    # Steam answered with its throttle page: the budget is spent, the item is
    # fine. Buys the long pause, not the delay rule.
    RATE_LIMITED = "rate_limited"
    # The Workshop says the item is not there: clear its queue flag and stop.
    ITEM_MISSING = "item_missing"
    # The item's own page was served but has no extended description: permanent,
    # so clear the queue flag; neutral for pacing.
    ITEM_PAGE_WITHOUT_DESCRIPTION = "item_page_without_description"
    # An age check, a sign-in wall, or Steam's error shell: a session or wall
    # problem that ``_refresh_login_cookie_if_gated_or_signed_out`` already re-reads the login
    # cookie for. Not a pacing problem.
    GATED = "gate"
    # An outcome we cannot attribute: a transport failure or a page that is
    # neither the item's nor a recognised condition. The only served outcome
    # that grows ``web_delay``.
    UNKNOWN = "unknown"


def classify_scrape(scrape_result: dict | None) -> ScrapeOutcome:
    """Name one scrape attempt's outcome.

    The order encodes precedence, and each step exists for a reason:

    * The description is the success test; every other result is a miss.
    * ``None`` is a transport failure and is unattributable by construction.
    * A 404/410 means the item is gone. It is checked before the gate predicate
      because Steam serves its item-error page from the ordinary error shell
      ("Steam Community :: Error"), which ``looks_like_gated`` claims.
    * A 5xx is a server fault with no attributable cause, so it is unknown
      whatever the body happens to contain -- including that same wording.
    * Failing a status, Steam's item-error wording on a served 200 page means
      the item is gone too (``looks_like_missing_item``).
    * A throttle page outranks the remaining page predicates: it is not the
      item, and its answer is the pause rather than the back-off.
    * A served item page whose description element is absent is the one miss
      that is the item's own doing.
    * Anything left is a page that is not the item's and matches no recognised
      condition, i.e. unknown.
    """
    if scrape_result is None:
        return ScrapeOutcome.UNKNOWN
    if scrape_result.get("description") is not None:
        return ScrapeOutcome.SUCCESS

    status = scrape_result.get("http_status")
    body = scrape_result.get("body") or ""
    if status in ITEM_MISSING_HTTP_STATUSES:
        return ScrapeOutcome.ITEM_MISSING
    if isinstance(status, int) and status >= 500:
        return ScrapeOutcome.UNKNOWN
    if looks_like_missing_item(body):
        return ScrapeOutcome.ITEM_MISSING
    if looks_like_rate_limited(body):
        return ScrapeOutcome.RATE_LIMITED
    if looks_like_item_page_without_description(body):
        return ScrapeOutcome.ITEM_PAGE_WITHOUT_DESCRIPTION
    if looks_like_gated(body):
        return ScrapeOutcome.GATED
    return ScrapeOutcome.UNKNOWN


class WebScraperThread(threading.Thread):
    def __init__(self, db_path: str, pause_lock_file: str, state_store=None,
                 session_refresh=None):
        super().__init__(daemon=True)
        self.db_path = db_path
        self.pause_lock_file = pause_lock_file
        # Where this worker's delay is persisted. None means "no state file" --
        # a test or an embedded construction -- and it then behaves exactly as
        # it did before the delay moved out of config: it moves in memory only.
        self._state_store = state_store
        # Re-reads the browser login cookie and reports whether it changed. None
        # when no browser source is configured.
        self._session_refresh = session_refresh
        self.running = True
        # The starting delay is the one persisted when the worker last ran, so a
        # restart resumes at the delay it had reached; the default applies only
        # when there is no state at all.
        stored = pacing.load_delay(state_store, pacing.WEB_DELAY_SECTION)
        self.web_delay = stored if stored is not None else WEB_DELAY_DEFAULT
        self.web_successes = 0
        self.web_failures = 0
        self.web_had_success_streak = False
        # In memory only: a daemon restarted after a day resumes at the delay it
        # had reached rather than treating the downtime as healthy operation.
        self._clock = pacing.Clock()
        self._persisted_web_delay = self.web_delay

    def _refresh_login_cookie_if_gated_or_signed_out(self, item: dict, scrape_result: dict | None) -> None:
        """Refresh the login cookie when a failed scrape looks gated.

        A gated page and a changed layout are indistinguishable from the selector
        alone, but only one of them can be fixed by a fresher login cookie. The
        cookie is valid for days, so it is not polled on a clock; this is the
        evidence that it has gone stale. The refresh is a local file read through
        the injected ``session_refresh`` and spends no request, so it happens
        even for a throttle page and the next request that does go out carries
        the freshest credential.

        It deliberately does **not** re-scrape. The miss goes through
        ``classify_scrape`` and takes the ordinary outcome path -- ``_handle_gate``
        leaves the item queued in its place, and a rate limit or unknown outcome
        applies its usual back-off -- so the queue retries the item under the
        worker's own adaptive delay, which is the only spacing a request pays. A
        definitive HTTP 404/410 is exempt from the refresh: the item is gone and
        no credential changes that.
        """
        if not scrape_result or scrape_result.get("description") is not None:
            return
        # A definitive HTTP 404/410 is not a gate and no cookie refresh can
        # materialise the item, so it is not worth the file read. The HTTP 200
        # item-error page is not caught here: its status proves nothing, so a
        # stale session is still worth ruling out before the outcome is read as
        # "the item is gone".
        if scrape_result.get("http_status") in ITEM_MISSING_HTTP_STATUSES:
            return
        body = scrape_result.get("body") or ""

        # Both conditions are evaluated, and neither shadows the other. They
        # overlap by construction -- a throttle page is not the item page, so it
        # lacks the signed-in markers too -- and reading that overlap as "signed
        # out" would be a mistake in one direction and skipping the refresh a
        # mistake in the other.
        if self._session_refresh and (looks_like_signed_out(body) or looks_like_gated(body)):
            try:
                self._session_refresh()
            except Exception as exc:
                logging.warning("[W:%s] Login cookie refresh failed: %s", item.get("workshop_id"), exc)

        # A throttle page is anonymous because the budget is spent, not because
        # the login is bad, so it makes no claim about the session in either
        # direction. Every other miss does: this records the problem, or clears
        # one the fresh page disproves.
        if not looks_like_rate_limited(body):
            self._record_session_health_from(body)

    def _record_session_health_from(self, body: str) -> None:
        """Record or clear the login problem a failed scrape is evidence of.

        The predicate is :func:`looks_like_signed_out`, the specific one, rather than
        the broader gate predicate beside it: it is what this codebase already
        trusts for exactly this decision, and unlike the gate predicate it does
        not fire on a throttle page. It is a heuristic and is documented as one --
        a withheld page that arrived without Steam's header (an error shell, an
        age check) reads the same way -- which is affordable because the warning
        names the remedy and the recheck button clears it from the token alone.

        An empty body claims nothing in either direction. This runs on the
        scraped page rather than on the cookie alone because Steam's answer is
        what settles the question -- a token can look valid and still be refused.
        """
        if not body:
            return
        if looks_like_signed_out(body):
            session_health.record_rejected(self.db_path, session_health.NOT_ACCEPTED_DETAIL)
        else:
            session_health.record_accepted(self.db_path)

    def _decay_delay(self, elapsed: float) -> None:
        """Shrink the delay for the healthy time since the previous attempt.

        The interval is wall-clock, so the delay halves over
        `pacing.HALF_LIFE_SECONDS` whatever its size. The old rule needed 100
        consecutive successes, which is a different amount of elapsed time at
        every delay and so recovered faster the faster it was already going.
        """
        old = self.web_delay
        self.web_delay = pacing.decay(self.web_delay, elapsed, WEB_DELAY_FLOOR)
        if old != self.web_delay:
            self._persist_delay()

    def _persist_delay(self, force: bool = False) -> None:
        """Write the delay into its state section when it has moved far enough.

        The decay runs on every success now, so without a step of its own it
        would rewrite the state file once per scrape. The state file is the home
        the config file used to provide, and the step bound is unchanged.
        """
        if not force and not pacing.needs_persist(
                self.web_delay, self._persisted_web_delay):
            return
        self._persisted_web_delay = self.web_delay
        pacing.save_delay(self._state_store, pacing.WEB_DELAY_SECTION, self.web_delay)

    def _record_web_failure(self) -> None:
        """Count one scrape whose outcome we cannot attribute, and grow the delay.

        Only ``ScrapeOutcome.UNKNOWN`` reaches here: a transport failure, or a
        page that is neither the item's nor a recognised condition. Every other
        outcome has a response that a slower pace cannot improve -- a throttle
        has its own pause, a missing item and a description-less item page are
        answers the item itself gave, and a gate is a session problem
        ``_refresh_login_cookie_if_gated_or_signed_out`` already re-reads the login cookie for
        -- so none of them buys a back-off.
        """
        self.web_failures += 1
        self.web_successes = 0
        if self.web_failures >= 2 and self.web_had_success_streak:
            old = self.web_delay
            self.web_delay = pacing.backoff(self.web_delay)
            logging.info(f"Multiple consecutive web scrape failures! Increasing web delay from {old} to {self.web_delay}s.")
            # Always written: a restart during an outage must not resume at the
            # pace that was just refused.
            self._persist_delay(force=True)
            self.web_had_success_streak = False

    def _clear_web_scrape_flag(self, workshop_id: int) -> None:
        """Take an item out of the web queue without touching any other flag."""
        conn = get_connection(self.db_path)
        conn.execute(
            "UPDATE workshop_items SET web_scrape_priority = 0 WHERE workshop_id = ?",
            (workshop_id,)
        )
        conn.commit()
        conn.close()

    def _capture_scrape_failure(self, item: dict, url: str, scrape_result: dict,
                                failure_kind: str) -> None:
        """Record the served page as evidence for whichever miss it was.

        ``failure_kind`` is required: a default here filed three unrelated misses under
        ``web_selector_miss``, which is exactly the distinction the failure tree
        exists to preserve. The selector is recorded only for the kind that is
        about the selector.
        """
        capture.record_failure(
            kind=failure_kind,
            stage="web_scrape",
            workshop_id=item["workshop_id"],
            selector=DESCRIPTION_SELECTOR if failure_kind == "web_selector_miss" else None,
            http_status=scrape_result.get("http_status"),
            final_url=scrape_result.get("final_url") or url,
            body=scrape_result.get("body"),
            content_type="text/html",
        )

    def _handle_item_page_without_description(self, item: dict, url: str,
                                              scrape_result: dict) -> None:
        """Handle an item page that was served but carries no description.

        ``scrape_extended_details`` returns ``{"description": None, "tags": []}``
        on a miss, which is truthy. The previous code took that as success and
        wrote ``extended_description = NULL`` with ``web_scrape_priority = 0``, so
        the item was recorded as permanently scraped with nothing to show for it.
        The markup now decides: the template is present and the description
        element is not, so the page really is the item's and the absence is
        permanent. Clearing ``web_scrape_priority`` is what lets the queue drain
        instead of retrying a page that will never carry a description.

        Neutral for pacing: the request succeeded, so there is nothing to back
        off from, but it yielded nothing, so it must not reset the failure streak
        either.
        """
        self._capture_scrape_failure(item, url, scrape_result,
                                     failure_kind="web_description_absent")
        logging.warning(
            "[W:%s] Item page has no extended description; clearing web_scrape_priority",
            item["workshop_id"])
        self._clear_web_scrape_flag(item["workshop_id"])

    def _handle_missing_item(self, item: dict, url: str, scrape_result: dict) -> None:
        """Handle the Workshop saying the item is not there.

        Steam serves this from its ordinary error shell, and a live probe shows
        the status is **HTTP 200** for a well-formed but absent id, so the status
        is quoted when there is one and the page's own wording is quoted as well.
        ``scraper.log`` is the owner's only view, and until this outcome was
        distinguished a missing item and a timeout read identically.

        The row is deliberately **not** marked dead: existence is the API's call,
        and the API makes it on its own 404. Clearing ``web_scrape_priority`` is the
        conservative move -- the API re-flags the item while its description is
        still missing if Steam ever serves it again -- and it is what stops a
        gone item spinning in the queue at full pace now that a missing item no
        longer backs off.
        """
        workshop_id = item["workshop_id"]
        status = scrape_result.get("http_status")
        reason = missing_item_reason(scrape_result.get("body") or "")
        self._capture_scrape_failure(item, url, scrape_result, failure_kind="web_item_missing")
        status_text = f"HTTP {status}" if status is not None else "no status code"
        evidence = f"; page said {reason!r}" if reason else ""
        logging.warning(
            "[W:%s] Workshop item is not available (%s%s); clearing "
            "web_scrape_priority and not backing off -- the API remains the "
            "authority on whether the item exists.",
            workshop_id, status_text, evidence)
        self._clear_web_scrape_flag(workshop_id)

    def _handle_gate(self, item: dict, url: str, scrape_result: dict) -> None:
        """Handle a wall, an age check, or Steam's error shell.

        ``_refresh_login_cookie_if_gated_or_signed_out`` has already re-read the login cookie
        when the page looked gated or signed out, so the next attempt carries a
        fresher credential. Nothing was re-scraped immediately: the miss takes
        this ordinary path, the item keeps its queue place because it is not at
        fault, and the queue retries it under the worker's own delay. A slower
        request rate cannot fix a session that is not working, so the delay is
        left alone.
        """
        self._capture_scrape_failure(item, url, scrape_result, failure_kind="web_gated")
        logging.warning(
            "[W:%s] Page looks gated (no item markup; HTTP %s); leaving the item "
            "queued and the delay unchanged.",
            item["workshop_id"], scrape_result.get("http_status"))

    def _handle_unknown(self, item: dict, url: str, scrape_result: dict | None) -> None:
        """Handle an outcome we cannot attribute, and back off.

        A transport failure (``scrape_result`` is ``None``) also raises
        ``api_priority`` to 2 so the metadata is re-fetched before the web scrape
        is tried again. For a page that did reach us, the request got through, so
        only the pacing changes.
        """
        workshop_id = item["workshop_id"]
        if scrape_result is None:
            logging.warning(
                "[W:%s] Web scrape failed with no response (transport failure); "
                "backing off.", workshop_id)
            conn = get_connection(self.db_path)
            # The whole statement skips a dead row, deliberately: unlike the
            # image worker's two-term UPDATE there is no other column here, so a
            # `fetch_status = -1` predicate in the WHERE *is* the `api_priority`
            # guard and it leaves the row unwritten. A dead item is final and in
            # no queue; the API poll excludes dead rows, so this bump could never
            # be handed out and would only strand the row in `dead_queued`
            # (issue 74).
            conn.execute(
                "UPDATE workshop_items SET api_priority = "
                "CASE WHEN api_priority < 2 THEN 2 ELSE api_priority END "
                "WHERE workshop_id = ? "
                "AND (fetch_status IS NULL OR fetch_status != -1)",
                (workshop_id,)
            )
            conn.commit()
            conn.close()
        else:
            self._capture_scrape_failure(item, url, scrape_result, failure_kind="web_unknown")
            logging.warning(
                "[W:%s] Page was not the item's and matched no known condition "
                "(HTTP %s); leaving web_scrape_priority unchanged and backing off.",
                workshop_id, scrape_result.get("http_status"))
        self._record_web_failure()

    def run(self):
        logging.info("Web scraper thread started.")
        while self.running:
            # Only the web thread pauses when TUI locks
            while os.path.exists(self.pause_lock_file) and self.running:
                time.sleep(1)

            item = None
            try:
                item = get_next_web_scrape_item(self.db_path)
                if not item:
                    # Responsive, so an idle worker is not deaf to a stop for the
                    # whole nap. The blind ten-second sleep this replaced was why a
                    # shutdown with nothing queued had to wait out the daemon's join
                    # timeout: the stop flag was set, and the worker would not look
                    # at it until the sleep ended.
                    pacing.wait(10.0, lambda: self.running)
                    continue

                workshop_id = item["workshop_id"]
                url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"
                # When web-download capture is on, ask for the body even on
                # success: the whole point is to see what a working, signed-in page
                # looks like. One interval per attempt, advanced whether or not it
                # succeeds; a failure must not leave it running, or the next success
                # would read the whole outage as elapsed time and collapse the delay
                # at once.
                elapsed = self._clock.since()
                capture_body = capture.web_download_capture_active()
                scrape_result = scrape_extended_details(url, keep_body=capture_body)
                self._refresh_login_cookie_if_gated_or_signed_out(item, scrape_result)
                if capture_body and scrape_result:
                    capture.record_web_download(
                        capture.ITEM_PAGE_KIND, workshop_id, url, scrape_result,
                        succeeded=scrape_result.get("description") is not None)

                outcome = classify_scrape(scrape_result)
                if outcome is ScrapeOutcome.SUCCESS:
                    item_update = {
                        "workshop_id": workshop_id,
                        "extended_description": scrape_result.get("description"),
                        "web_scrape_priority": 0,
                        # Our clock, taken now that the page is in hand. Only this
                        # success branch writes it; a miss, a wall or a transport
                        # failure leaves the previous completion time, or NULL,
                        # alone.
                        "web_scraped_at": int(time.time()),
                    }
                    insert_or_update_item(self.db_path, item_update)

                    # Flag extended description for translation, unless the stored
                    # translation was taken at the item's current Steam revision.
                    desc = scrape_result.get("description") or ""
                    if desc and not translation_is_current(
                            item.get("extended_description_en"),
                            item.get("translate_version"),
                            item.get("steam_updated_at")):
                        queue_field_for_translation(self.db_path, "item", workshop_id, "extended_description_en", desc, 3)

                    title = item.get("title_en") or item.get("title") or str(workshop_id)
                    logging.info(f"[W:{workshop_id}] Scraped \"{title}\"")
                    self.web_successes += 1
                    self.web_failures = 0
                    if self.web_successes >= 5:
                        self.web_had_success_streak = True
                    self._decay_delay(elapsed)
                elif outcome is ScrapeOutcome.RATE_LIMITED:
                    # Not a bad item and not necessarily a stale cookie: the page
                    # itself reports too many requests, and that is the only thing
                    # observed. The item is left alone either way -- the reply is not
                    # the item -- so the cause stays unnamed.
                    #
                    # A throttle is a refusal, and an unambiguous one, so it halves
                    # the request rate immediately rather than waiting for the
                    # second strike an unattributable failure needs.
                    # same way any other refusal does. It used to sleep a fixed
                    # 300 s and leave the delay alone, which made it a second pacing
                    # rule that could not converge: the same pause every time,
                    # forever. The doubling is served by the wait at the end of this
                    # iteration, so a sustained throttle backs off geometrically.
                    old_delay = self.web_delay
                    self.web_delay = pacing.backoff(self.web_delay)
                    logging.warning(
                        "[W:%s] Steam returned a page reporting too many requests; "
                        "increasing the delay from %.2fs to %.2fs and leaving the "
                        "item untouched.",
                        workshop_id, old_delay, self.web_delay)
                    self._persist_delay(force=True)
                    self.web_failures += 1
                    self.web_successes = 0
                elif outcome is ScrapeOutcome.ITEM_MISSING:
                    self._handle_missing_item(item, url, scrape_result)
                elif outcome is ScrapeOutcome.ITEM_PAGE_WITHOUT_DESCRIPTION:
                    self._handle_item_page_without_description(item, url, scrape_result)
                elif outcome is ScrapeOutcome.GATED:
                    self._handle_gate(item, url, scrape_result)
                else:  # ScrapeOutcome.UNKNOWN
                    self._handle_unknown(item, url, scrape_result)
                # Responsive, so a long backoff cannot make the worker deaf to a
                # stop or a pause. It serves the delay in full; it does not shorten it.
                pacing.wait(self.web_delay, lambda: self.running)
            except sqlite3.OperationalError as exc:
                # A lock that outlived the busy timeout is one lost iteration.
                # The row is left exactly as it was -- this pass never reached
                # its write -- so it is still queued for the next attempt.
                if item is not None:
                    logging.warning(
                        "[W:%s] Database locked; leaving the item queued and "
                        "retrying in %gs: %s",
                        item["workshop_id"], pacing.DB_LOCK_RETRY_SECONDS, exc)
                else:
                    logging.warning(
                        "Web scraper could not read the queue (database "
                        "locked); retrying in %gs: %s",
                        pacing.DB_LOCK_RETRY_SECONDS, exc)
                # Responsive, so a stop is not held for the whole pause.
                pacing.wait(pacing.DB_LOCK_RETRY_SECONDS, lambda: self.running)

        self._persist_delay(force=True)
        # No "Web scraper thread stopped." here: the daemon logs one line per
        # worker as it confirms the join, and this thread's own copy made the
        # owner's log show the same sentence twice. See Daemon._join_workers.


