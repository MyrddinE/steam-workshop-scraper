"""Background thread for web scraping Steam Workshop pages."""

import enum
import time
import os
import logging
import threading
from datetime import datetime, timezone
from src.database import get_next_web_scrape_item, insert_or_update_item, get_connection, flag_field_for_translation, translation_is_current
from src.web_scraper import (DESCRIPTION_SELECTOR, ITEM_MISSING_HTTP_STATUSES, looks_gated,
                             looks_like_item_page_without_description, looks_like_missing_item,
                             looks_rate_limited, looks_signed_out, missing_item_reason,
                             scrape_extended_details)
from src import capture

# Steam's per-account request budget refills over minutes, so the useful
# response to its throttle page is a pause measured in minutes, not the
# seconds used for ordinary pacing.
RATE_LIMIT_PAUSE_SECONDS = 300.0

# The slowest the decay rule will take the scraper. The owner learned the figure
# empirically: the same Steam budget is shared with their own hand-browsing, so
# when scrapes start failing the worker has to back off far enough that the
# Workshop is still usable by hand while the daemon runs. The old 1.0 s floor
# left no room for that.
WEB_DELAY_FLOOR = 6.0

# The starting delay. Set to the floor rather than below it: a default under the
# floor would be a delay the decay rule considers too fast, and the first 100
# successes would rewrite it upward to the floor anyway.
WEB_DELAY_DEFAULT = WEB_DELAY_FLOOR


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
    # problem that ``_retry_if_gated`` already addresses. Not a pacing problem.
    GATE = "gate"
    # An outcome we cannot attribute: a transport failure or a page that is
    # neither the item's nor a recognised condition. The only served outcome
    # that grows ``web_delay``.
    UNKNOWN = "unknown"


def classify_scrape(scrape_data: dict | None) -> ScrapeOutcome:
    """Name one scrape attempt's outcome.

    The order encodes precedence, and each step exists for a reason:

    * The description is the success test; every other result is a miss.
    * ``None`` is a transport failure and is unattributable by construction.
    * A 404/410 means the item is gone. It is checked before the gate predicate
      because Steam serves its item-error page from the ordinary error shell
      ("Steam Community :: Error"), which ``looks_gated`` claims.
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
    if scrape_data is None:
        return ScrapeOutcome.UNKNOWN
    if scrape_data.get("description") is not None:
        return ScrapeOutcome.SUCCESS

    status = scrape_data.get("http_status")
    body = scrape_data.get("body") or ""
    if status in ITEM_MISSING_HTTP_STATUSES:
        return ScrapeOutcome.ITEM_MISSING
    if isinstance(status, int) and status >= 500:
        return ScrapeOutcome.UNKNOWN
    if looks_like_missing_item(body):
        return ScrapeOutcome.ITEM_MISSING
    if looks_rate_limited(body):
        return ScrapeOutcome.RATE_LIMITED
    if looks_like_item_page_without_description(body):
        return ScrapeOutcome.ITEM_PAGE_WITHOUT_DESCRIPTION
    if looks_gated(body):
        return ScrapeOutcome.GATE
    return ScrapeOutcome.UNKNOWN


class WebScraperThread(threading.Thread):
    def __init__(self, db_path: str, pause_lock_file: str, daemon_config: dict = None,
                 save_callback=None, session_refresh=None):
        super().__init__(daemon=True)
        self.db_path = db_path
        self.pause_lock_file = pause_lock_file
        self._save_cb = save_callback
        # Returns True when the login cookie changed, meaning a gated scrape
        # is worth retrying. None when no browser source is configured.
        self._session_refresh = session_refresh
        self.running = True
        self.web_delay = float((daemon_config or {}).get("web_delay_seconds") or WEB_DELAY_DEFAULT)
        self.web_successes = 0
        self.web_failures = 0
        self.web_had_streak = False

    def _wait_out_throttle(self, seconds: float) -> None:
        """Sleep in one-second steps so a pause cannot hold shutdown hostage."""
        deadline = time.monotonic() + seconds
        while self.running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    def _retry_if_gated(self, item: dict, url: str, scrape_data: dict | None) -> dict | None:
        """Retry once when a failed scrape looks like Steam withholding the page.

        A gated page and a changed layout are indistinguishable from the selector
        alone, but only one of them can be fixed by a fresher login cookie. The
        cookie is valid for days, so it is not polled on a clock; this is the
        evidence that it has gone stale. Nothing is retried unless the cookie
        actually changed, so a merely broken page cannot double the request rate.
        A definitive HTTP 404/410 is exempt: the item is gone and no credential
        changes that.
        """
        if not scrape_data or scrape_data.get("description") is not None:
            return scrape_data
        # A definitive HTTP 404/410 is not a gate and no cookie refresh can
        # materialise the item, so it is not worth a request. The HTTP 200
        # item-error page is not caught here: its status proves nothing, so a
        # stale session is still worth ruling out with the retry below before
        # the outcome is read as "the item is gone".
        if scrape_data.get("http_status") in ITEM_MISSING_HTTP_STATUSES:
            return scrape_data
        body = scrape_data.get("body") or ""

        # Both conditions are evaluated, and neither shadows the other. They
        # overlap by construction -- a throttle page is not the item page, so it
        # lacks the signed-in markers too -- and reading that overlap as "signed
        # out" would be a mistake in one direction and skipping the refresh a
        # mistake in the other.
        #
        # The throttle suppresses only the network retry. Refreshing the cookie
        # is a local file read that spends none of the exhausted budget, and
        # doing it here means the next request that does go out carries the
        # freshest credential.
        throttled = looks_rate_limited(body)
        signed_out = self._session_refresh and (looks_signed_out(body) or looks_gated(body))

        changed = False
        if signed_out:
            try:
                changed = self._session_refresh()
            except Exception as exc:
                logging.warning("[W:%s] Login cookie refresh failed: %s", item.get("workshop_id"), exc)

        if throttled:
            # Never retry into a budget that is already spent.
            return scrape_data
        if not changed:
            return scrape_data
        logging.info("[W:%s] Scrape looked gated; retrying with the refreshed login cookie",
                     item.get("workshop_id"))
        return scrape_extended_details(url) or scrape_data

    def _record_web_failure(self) -> None:
        """Count one scrape whose outcome we cannot attribute, and grow the delay.

        Only ``ScrapeOutcome.UNKNOWN`` reaches here: a transport failure, or a
        page that is neither the item's nor a recognised condition. Every other
        outcome has a response that a slower pace cannot improve -- a throttle
        has its own pause, a missing item and a description-less item page are
        answers the item itself gave, and a gate is a session problem
        ``_retry_if_gated`` already addresses -- so none of them buys a back-off.
        """
        self.web_failures += 1
        self.web_successes = 0
        if self.web_failures >= 2 and self.web_had_streak:
            old = self.web_delay
            self.web_delay = min(round(self.web_delay * (1.05 ** 10), 3),20)
            logging.info(f"Multiple consecutive web scrape failures! Increasing web delay from {old} to {self.web_delay}s.")
            if self._save_cb:
                self._save_cb("web_delay_seconds", self.web_delay)
            self.web_had_streak = False

    def _clear_web_scrape_flag(self, workshop_id: int) -> None:
        """Take an item out of the web queue without touching any other flag."""
        conn = get_connection(self.db_path)
        conn.execute(
            "UPDATE workshop_items SET needs_web_scrape = 0 WHERE workshop_id = ?",
            (workshop_id,)
        )
        conn.commit()
        conn.close()

    def _capture_scrape_failure(self, item: dict, url: str, scrape_data: dict,
                                kind: str = "web_selector_miss") -> None:
        """Record the served page as evidence for whichever miss it was."""
        capture.record_failure(
            kind=kind,
            stage="web_scrape",
            workshop_id=item["workshop_id"],
            selector=DESCRIPTION_SELECTOR if kind == "web_selector_miss" else None,
            http_status=scrape_data.get("http_status"),
            final_url=scrape_data.get("final_url") or url,
            body=scrape_data.get("body"),
            content_type="text/html",
        )

    def _handle_item_page_without_description(self, item: dict, url: str,
                                              scrape_data: dict) -> None:
        """Handle an item page that was served but carries no description.

        ``scrape_extended_details`` returns ``{"description": None, "tags": []}``
        on a miss, which is truthy. The previous code took that as success and
        wrote ``extended_description = NULL`` with ``needs_web_scrape = 0``, so
        the item was recorded as permanently scraped with nothing to show for it.
        The markup now decides: the template is present and the description
        element is not, so the page really is the item's and the absence is
        permanent. Clearing ``needs_web_scrape`` is what lets the queue drain
        instead of retrying a page that will never carry a description.

        Neutral for pacing: the request succeeded, so there is nothing to back
        off from, but it yielded nothing, so it must not reset the failure streak
        either.
        """
        self._capture_scrape_failure(item, url, scrape_data)
        logging.warning(
            "[W:%s] Item page has no extended description; clearing needs_web_scrape",
            item["workshop_id"])
        self._clear_web_scrape_flag(item["workshop_id"])

    def _handle_missing_item(self, item: dict, url: str, scrape_data: dict) -> None:
        """Handle the Workshop saying the item is not there.

        Steam serves this from its ordinary error shell, and a live probe shows
        the status is **HTTP 200** for a well-formed but absent id, so the status
        is quoted when there is one and the page's own wording is quoted as well.
        ``scraper.log`` is the owner's only view, and until this outcome was
        distinguished a missing item and a timeout read identically.

        The row is deliberately **not** marked dead: existence is the API's call,
        and the API makes it on its own 404. Clearing ``needs_web_scrape`` is the
        conservative move -- the API re-flags the item while its description is
        still missing if Steam ever serves it again -- and it is what stops a
        gone item spinning in the queue at full pace now that a missing item no
        longer backs off.
        """
        workshop_id = item["workshop_id"]
        status = scrape_data.get("http_status")
        reason = missing_item_reason(scrape_data.get("body") or "")
        self._capture_scrape_failure(item, url, scrape_data, kind="web_item_missing")
        status_text = f"HTTP {status}" if status is not None else "no status code"
        evidence = f"; page said {reason!r}" if reason else ""
        logging.warning(
            "[W:%s] Workshop item is not available (%s%s); clearing "
            "needs_web_scrape and not backing off -- the API remains the "
            "authority on whether the item exists.",
            workshop_id, status_text, evidence)
        self._clear_web_scrape_flag(workshop_id)

    def _handle_gate(self, item: dict, url: str, scrape_data: dict) -> None:
        """Handle a wall, an age check, or Steam's error shell.

        ``_retry_if_gated`` has already re-read the login cookie and retried once
        when the cookie actually changed. A slower request rate cannot fix a
        session that is not working, so this leaves the delay alone. The item
        keeps its queue place because it is not at fault.
        """
        self._capture_scrape_failure(item, url, scrape_data)
        logging.warning(
            "[W:%s] Page looks gated (no item markup; HTTP %s); leaving the item "
            "queued and the delay unchanged.",
            item["workshop_id"], scrape_data.get("http_status"))

    def _handle_unknown(self, item: dict, url: str, scrape_data: dict | None) -> None:
        """Handle an outcome we cannot attribute, and back off.

        A transport failure (``scrape_data`` is ``None``) also raises
        ``api_priority`` to 2 so the metadata is re-fetched before the web scrape
        is tried again. For a page that did reach us, the request got through, so
        only the pacing changes.
        """
        workshop_id = item["workshop_id"]
        if scrape_data is None:
            logging.warning(
                "[W:%s] Web scrape failed with no response (transport failure); "
                "backing off.", workshop_id)
            conn = get_connection(self.db_path)
            conn.execute(
                "UPDATE workshop_items SET api_priority = CASE WHEN api_priority < 2 THEN 2 ELSE api_priority END WHERE workshop_id = ?",
                (workshop_id,)
            )
            conn.commit()
            conn.close()
        else:
            self._capture_scrape_failure(item, url, scrape_data)
            logging.warning(
                "[W:%s] Page was not the item's and matched no known condition "
                "(HTTP %s); leaving needs_web_scrape unchanged and backing off.",
                workshop_id, scrape_data.get("http_status"))
        self._record_web_failure()

    def run(self):
        logging.info("Web scraper thread started.")
        while self.running:
            # Only the web thread pauses when TUI locks
            while os.path.exists(self.pause_lock_file) and self.running:
                time.sleep(1)

            item = get_next_web_scrape_item(self.db_path)
            if not item:
                time.sleep(10)
                continue

            workshop_id = item["workshop_id"]
            url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"
            # When scrape capture is on, ask for the body even on success: the
            # whole point is to see what a working, signed-in page looks like.
            keeping = capture.web_scrape_capture_active()
            scrape_data = scrape_extended_details(url, keep_body=keeping)
            scrape_data = self._retry_if_gated(item, url, scrape_data)
            if keeping:
                capture.record_web_scrape(workshop_id, url, scrape_data)

            outcome = classify_scrape(scrape_data)
            if outcome is ScrapeOutcome.SUCCESS:
                update = {
                    "workshop_id": workshop_id,
                    "extended_description": scrape_data.get("description"),
                    "needs_web_scrape": 0,
                    "scrape_version": item.get("steam_updated_at", 0),
                }
                insert_or_update_item(self.db_path, update)

                # Flag extended description for translation, unless the stored
                # translation was taken at the item's current Steam revision.
                desc = scrape_data.get("description") or ""
                if desc and not translation_is_current(
                        item.get("extended_description_en"),
                        item.get("translate_version"),
                        item.get("steam_updated_at")):
                    flag_field_for_translation(self.db_path, "item", workshop_id, "extended_description_en", desc, 3)

                display = item.get("title_en") or item.get("title") or str(workshop_id)
                logging.info(f"[W:{workshop_id}] Scraped \"{display}\"")
                self.web_successes += 1
                self.web_failures = 0
                if self.web_successes >= 5:
                    self.web_had_streak = True
                if self.web_successes >= 100:
                    old = self.web_delay
                    self.web_delay = max(WEB_DELAY_FLOOR, round(self.web_delay / 1.05, 3))
                    if old != self.web_delay:
                        logging.info(f"100 consecutive web successes! Decreasing web delay from {old} to {self.web_delay}s.")
                        if self._save_cb:
                            self._save_cb("web_delay_seconds", self.web_delay)
                    self.web_successes = 0
            elif outcome is ScrapeOutcome.RATE_LIMITED:
                # Not a bad item and not necessarily a stale cookie: the page
                # itself reports too many requests, and that is the only thing
                # that has actually been observed. Acting on it is right either
                # way -- the reply is not the item -- so the cause is left
                # unnamed. Decaying the item would blame the wrong thing, and a
                # retry would spend more of whatever the budget is. The pause is
                # the whole response; the delay rule is not touched.
                logging.warning(
                    "[W:%s] Steam returned a page reporting too many requests; "
                    "pausing %.0fs and leaving the item untouched.",
                    workshop_id, RATE_LIMIT_PAUSE_SECONDS)
                self._wait_out_throttle(RATE_LIMIT_PAUSE_SECONDS)
                continue
            elif outcome is ScrapeOutcome.ITEM_MISSING:
                self._handle_missing_item(item, url, scrape_data)
            elif outcome is ScrapeOutcome.ITEM_PAGE_WITHOUT_DESCRIPTION:
                self._handle_item_page_without_description(item, url, scrape_data)
            elif outcome is ScrapeOutcome.GATE:
                self._handle_gate(item, url, scrape_data)
            else:  # ScrapeOutcome.UNKNOWN
                self._handle_unknown(item, url, scrape_data)
            time.sleep(self.web_delay)

        if self._save_cb:
            self._save_cb("web_delay_seconds", self.web_delay)
        logging.info("Web scraper thread stopped.")


