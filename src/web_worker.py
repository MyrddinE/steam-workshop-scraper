"""Background thread for web scraping Steam Workshop pages."""

import time
import os
import logging
import threading
from datetime import datetime, timezone
from src.database import get_next_web_scrape_item, insert_or_update_item, get_connection, flag_field_for_translation, translation_is_current
from src.web_scraper import (DESCRIPTION_SELECTOR, looks_gated, looks_like_item_page_without_description,
                             looks_rate_limited, looks_signed_out, scrape_extended_details)
from src import capture

# Steam's per-account request budget refills over minutes, so the useful
# response to its throttle page is a pause measured in minutes, not the
# seconds used for ordinary pacing.
RATE_LIMIT_PAUSE_SECONDS = 300.0


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
        self.web_delay = float((daemon_config or {}).get("web_delay_seconds") or 5.0)
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
        """
        if not scrape_data or scrape_data.get("description") is not None:
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
        """Count one request the server did not serve, and grow the delay.

        Shared by a transport failure and a page that was not the item's: the
        two differ in what happens to the item, not in how fast the scraper may
        knock, so both move the same counters and the same rule governs the
        delay. A page that was the item's and simply had no description is
        neither -- see ``_handle_selector_miss``.
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

    def _handle_selector_miss(self, item: dict, url: str, scrape_data: dict) -> bool:
        """Handle a page that loaded but whose description selector did not match.

        scrape_extended_details returns ``{"description": None, "tags": []}`` on a
        miss, which is truthy. The previous code took that as success and wrote
        ``extended_description = NULL`` with ``needs_web_scrape = 0``, so the item
        was recorded as permanently scraped with nothing to show for it (issue 19).

        A miss now means one of two things, told apart by the markup:

        * The item page was never served -- neither ``workshopItem`` nor
          ``highlightContent`` is present. The item is not at fault, so its queue
          priority is left exactly as it is: stepping it down, or clearing it,
          would blame the item for a wall, an error page, or a throttle page whose
          wording the rate-limit marker missed.
        * The item page was served but has no extended description -- the template
          is present and the description element is not. Some Workshop items
          genuinely have none, so this is permanent: clearing ``needs_web_scrape``
          lets the queue drain instead of retrying a page that will never carry
          one.

        The response is captured as evidence in both cases, since either may be a
        layout change worth knowing about. Neither raises ``api_priority``: the
        request reached Steam and came back, so this is not a transport failure,
        and a broken selector is not fixed by fetching the metadata again.

        The two cases are opposites for pacing, which is why the caller is told
        which one happened. A page that was not the item's is a request the server
        declined to serve -- the reason to slow down -- so the caller counts it as
        a failure. The item page with no description is neutral: the request
        succeeded, so there is nothing to back off from, but it yielded nothing,
        so it must not count as a success and reset the failure streak either.

        Returns ``True`` when the served body really was the item's page, and
        ``False`` when it was not. Only the caller updates the pacing counters and
        sleeps, so both failed outcomes grow the delay through one rule.
        """
        workshop_id = item["workshop_id"]
        capture.record_failure(
            kind="web_selector_miss",
            stage="web_scrape",
            workshop_id=workshop_id,
            selector=DESCRIPTION_SELECTOR,
            http_status=scrape_data.get("http_status"),
            final_url=scrape_data.get("final_url") or url,
            body=scrape_data.get("body"),
            content_type="text/html",
        )

        body = scrape_data.get("body") or ""
        if not looks_like_item_page_without_description(body):
            logging.warning(
                "[W:%s] Selector %s did not match and the body is not the item's "
                "page; leaving needs_web_scrape unchanged and backing off",
                workshop_id, DESCRIPTION_SELECTOR)
            return False

        logging.warning(
            "[W:%s] Item page has no extended description; clearing needs_web_scrape",
            workshop_id)
        conn = get_connection(self.db_path)
        conn.execute(
            "UPDATE workshop_items SET needs_web_scrape = 0 WHERE workshop_id = ?",
            (workshop_id,)
        )
        conn.commit()
        conn.close()
        return True

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

            if scrape_data and scrape_data.get("description") is not None:
                update = {
                    "workshop_id": workshop_id,
                    "extended_description": scrape_data.get("description"),
                    "needs_web_scrape": 0,
                    "scrape_version": item.get("steam_updated_at", 0),
                }
                insert_or_update_item(self.db_path, update)

                # Flag extended description for translation, unless the stored
                # translation was taken at the item's current Steam revision.
                # A selector miss is handled separately below and does not reach
                # here (scrape_data is None on a request failure).
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
                    self.web_delay = max(1.0, round(self.web_delay / 1.05, 3))
                    if old != self.web_delay:
                        logging.info(f"100 consecutive web successes! Decreasing web delay from {old} to {self.web_delay}s.")
                        if self._save_cb:
                            self._save_cb("web_delay_seconds", self.web_delay)
                    self.web_successes = 0
                time.sleep(self.web_delay)
            elif scrape_data is not None:
                if looks_rate_limited(scrape_data.get("body") or ""):
                    # Not a bad item and not necessarily a stale cookie: the
                    # page itself reports too many requests, and that is the
                    # only thing that has actually been observed. Acting on it
                    # is right either way -- the reply is not the item -- so the
                    # cause is left unnamed. Decaying the item would blame the
                    # wrong thing, and a retry would spend more of whatever the
                    # budget is.
                    logging.warning(
                        "[W:%s] Steam returned a page reporting too many requests; "
                        "pausing %.0fs and leaving the item untouched.",
                        workshop_id, RATE_LIMIT_PAUSE_SECONDS)
                    self._wait_out_throttle(RATE_LIMIT_PAUSE_SECONDS)
                    continue
                if not self._handle_selector_miss(item, url, scrape_data):
                    # The body was not the item's page: a wall, an error page,
                    # or a throttle the marker missed. The item stays queued
                    # because it is not at fault, but the scraper is not being
                    # served content, so it backs off through the same rule as
                    # a transport failure rather than knocking on regardless.
                    self._record_web_failure()
                time.sleep(self.web_delay)
            else:
                logging.warning(f"[W:{workshop_id}] Web scrape failed (no data returned)")
                conn = get_connection(self.db_path)
                conn.execute(
                    "UPDATE workshop_items SET api_priority = CASE WHEN api_priority < 2 THEN 2 ELSE api_priority END WHERE workshop_id = ?",
                    (workshop_id,)
                )
                conn.commit()
                conn.close()
                self._record_web_failure()
                time.sleep(self.web_delay)

        if self._save_cb:
            self._save_cb("web_delay_seconds", self.web_delay)
        logging.info("Web scraper thread stopped.")


