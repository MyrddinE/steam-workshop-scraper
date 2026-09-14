"""Database statistics as named metrics, each costed and tiered on its own.

One monolithic function used to compute every statistic for every caller, so the
cheapest consumer paid for the most expensive one: the tag endpoint ran the whole
payload — including a full-table scan classified in Python — to return one field.

Splitting the statistics into named metrics lets each tier be requested and
delivered separately, so a caller that only needs the item counts does not wait
for the tag join, and the front ends can draw the cheap numbers while the
expensive ones are still being computed.

Two rules keep this honest:

* **A metric owns one question.** If two numbers are always wanted together and
  always cost the same, they are one metric; otherwise they are two.
* **Semantics do not change here.** This module returns exactly the statistics
  `get_db_stats` returned before it existed. Only how they are computed and
  delivered is different — moving the per-item classification into SQL changes
  the cost, not the answer.

Tiers are named for their measured cost on a 1.7M-item database, not for how
they feel:

===========  ==============  ==========================================
Tier         Cost            Members
===========  ==============  ==========================================
``instant``  under 10 ms     totals, high-water mark, app tracking
``fast``     50-80 ms        status counts, fetch recency, coverage, stuck work
``slow``     hundreds of ms  translation status, priority mix, tags
===========  ==============  ==========================================
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from src.database import get_connection

INSTANT = "instant"
FAST = "fast"
SLOW = "slow"

#: Ordered cheapest first, which is the order a front end should draw them in.
TIERS = (INSTANT, FAST, SLOW)

STALENESS_DAYS = 30


@dataclass(frozen=True)
class Metric:
    """One named statistic: how to compute it, and what it costs to ask."""

    name: str
    tier: str
    note: str
    run: Callable[[Any, dict], Any]


REGISTRY: dict[str, Metric] = {}


def metric(name: str, tier: str, note: str):
    """Register a function as a named metric.

    Every metric is called as ``fn(conn, params)``. Most ignore ``params``; it
    carries the few knobs that would otherwise have to be module globals.
    """
    if tier not in TIERS:
        raise ValueError(f"{name}: unknown tier {tier!r}, expected one of {TIERS}")

    def decorate(fn):
        if name in REGISTRY:
            raise ValueError(f"duplicate metric {name!r}")
        REGISTRY[name] = Metric(name=name, tier=tier, note=note, run=fn)
        return fn

    return decorate


def names_in(tier: str) -> list[str]:
    """Metric names belonging to one tier, in registration order."""
    return [m.name for m in REGISTRY.values() if m.tier == tier]


def all_names() -> list[str]:
    """Every metric name, cheapest tier first."""
    return [name for tier in TIERS for name in names_in(tier)]


# --------------------------------------------------------------------------
# instant
# --------------------------------------------------------------------------


@metric("totals", INSTANT, "Item counts, split by whether the item is still alive.")
def _totals(conn, params) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       COALESCE(SUM(CASE WHEN status = -1 THEN 1 ELSE 0 END), 0) AS dead "
        "FROM workshop_items"
    ).fetchone()
    total = row["total"] or 0
    dead = row["dead"] or 0
    return {"total": total, "dead": dead, "alive": total - dead}


@metric("high_water", INSTANT, "The most recent successful API fetch.")
def _high_water(conn, params):
    return conn.execute("SELECT MAX(api_fetched_at) AS v FROM workshop_items").fetchone()["v"]


@metric("app_tracking", INSTANT, "Discovery position per application.")
def _app_tracking(conn, params) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT appid, last_page_scanned, last_cursor FROM app_tracking"
        )
    ]


# --------------------------------------------------------------------------
# fast
# --------------------------------------------------------------------------


@metric("status_counts", FAST, "Rows grouped by Steam's status code.")
def _status_counts(conn, params) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT status, COUNT(*) AS count FROM workshop_items GROUP BY status"
        )
    ]


@metric("coverage", FAST, "How much of the live library each stage has reached.")
def _coverage(conn, params) -> dict:
    """Processing coverage over live items.

    This is the progress view: outstanding depth says how much is queued, but
    only coverage says how far along the library actually is. Dead items are
    excluded because they will never be covered, and counting them would make
    coverage fall as the library is cleaned up.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN api_fetched_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS api_fetched,
               COALESCE(SUM(CASE WHEN COALESCE(extended_description, '') <> '' THEN 1 ELSE 0 END), 0) AS described,
               COALESCE(SUM(CASE WHEN COALESCE(image_extension, '') <> '' THEN 1 ELSE 0 END), 0) AS imaged,
               COALESCE(SUM(CASE WHEN translate_version IS NOT NULL THEN 1 ELSE 0 END), 0) AS translated,
               COALESCE(SUM(CASE WHEN COALESCE(creator, '') <> '' THEN 1 ELSE 0 END), 0) AS attributed
        FROM workshop_items
        WHERE status IS NULL OR status <> -1
        """
    ).fetchone()
    total = row["total"] or 0
    return {
        "total": total,
        "api_fetched": row["api_fetched"],
        "described": row["described"],
        "imaged": row["imaged"],
        "translated": row["translated"],
        "attributed": row["attributed"],
    }


@metric("stuck_work", FAST, "Dead items still sitting in a work queue.")
def _stuck_work(conn, params) -> dict:
    """Work queued against items that are already known to be gone.

    A dead item is marked by clearing `api_priority`, but the other queue flags
    are left set, and the web and image polls select on those flags without a
    dead-item guard. Those rows can never complete, so the queue never drains.
    Reporting them is how that stays visible instead of being quietly excluded
    from the backlog count.
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN needs_web_scrape > 0 THEN 1 ELSE 0 END), 0) AS web,
               COALESCE(SUM(CASE WHEN needs_image > 0 THEN 1 ELSE 0 END), 0) AS image,
               COALESCE(SUM(CASE WHEN translation_priority > 0 THEN 1 ELSE 0 END), 0) AS translation,
               COALESCE(SUM(CASE WHEN api_priority > 0 THEN 1 ELSE 0 END), 0) AS api
        FROM workshop_items
        WHERE status = -1
        """
    ).fetchone()
    return {k: row[k] for k in ("web", "image", "translation", "api")}


@metric("fetch_recency", FAST, "Our fetch recency, by last_fetch_attempted_at.")
def _fetch_recency(conn, params) -> dict:
    staleness_days = int(params.get("staleness_days", STALENESS_DAYS))
    threshold = int(time.time()) - staleness_days * 86400
    counts = {"fresh": 0, "stale": 0, "blank": 0}
    rows = conn.execute(
        """
        SELECT CASE
                 WHEN typeof(last_fetch_attempted_at) NOT IN ('integer', 'real') THEN 'blank'
                 WHEN last_fetch_attempted_at IS NULL OR last_fetch_attempted_at = 0 THEN 'blank'
                 WHEN last_fetch_attempted_at >= ? THEN 'fresh'
                 ELSE 'stale'
               END AS bucket,
               COUNT(*) AS cnt
        FROM workshop_items
        GROUP BY bucket
        """,
        (threshold,),
    )
    for row in rows:
        counts[row["bucket"]] = row["cnt"]
    return counts


# --------------------------------------------------------------------------
# slow
# --------------------------------------------------------------------------

# `length(CAST(x AS BLOB))` counts UTF-8 bytes and `length(x)` counts characters,
# so they agree exactly when every character is single-byte -- that is, when the
# text is ASCII. Python's str.isascii() is the same test, and this reproduces it
# for control characters too, which a GLOB over printable ASCII would not.
_IS_ASCII = "length(CAST(COALESCE({c}, '') AS BLOB)) = length(COALESCE({c}, ''))"


@metric("translation_status", SLOW, "Translation state, classified in SQL rather than per row in Python.")
def _translation_status(conn, params) -> dict:
    all_ascii = " AND ".join(
        _IS_ASCII.format(c=c)
        for c in ("title", "short_description", "extended_description")
    )
    counts = {
        "No translation needed (ASCII)": 0,
        "Needs Translation (Unicode)": 0,
        "Queued": 0,
        "Translated": 0,
        "No data (never scraped)": 0,
    }
    rows = conn.execute(
        f"""
        SELECT CASE
                 WHEN COALESCE(title_en, '') <> ''
                   OR COALESCE(short_description_en, '') <> ''
                   OR COALESCE(extended_description_en, '') <> '' THEN 'Translated'
                 WHEN {all_ascii} THEN 'No translation needed (ASCII)'
                 WHEN COALESCE(translation_priority, 0) > 0 THEN 'Queued'
                 WHEN COALESCE(title, '') = '' THEN 'No data (never scraped)'
                 ELSE 'Needs Translation (Unicode)'
               END AS bucket,
               COUNT(*) AS cnt
        FROM workshop_items
        GROUP BY bucket
        """
    )
    for row in rows:
        counts[row["bucket"]] = row["cnt"]
    return counts


@metric("priority_breakdowns", SLOW, "Fetchable work per queue, by priority.")
def _priority_breakdowns(conn, params) -> dict:
    """Outstanding depth and priority mix, excluding items that cannot complete.

    Dead items are left flagged by `src/daemon.py`, so counting them here
    reported a backlog that no worker could ever drain. They are counted by
    `stuck_work` instead, where the number means what it says.
    """
    out = {}
    for column in ("translation_priority", "needs_image", "needs_web_scrape"):
        out[column] = [
            dict(r)
            for r in conn.execute(
                f"SELECT {column} AS prio, COUNT(*) AS cnt FROM workshop_items "
                f"WHERE {column} > 0 AND (status IS NULL OR status <> -1) "
                f"GROUP BY {column} ORDER BY prio DESC"
            )
        ]
    return out


@metric("tag_counts", SLOW, "Tag frequencies from the junction table.")
def _tag_counts(conn, params) -> dict:
    return {
        row["tag_name"]: row["cnt"]
        for row in conn.execute(
            "SELECT t.tag_name, COUNT(*) AS cnt "
            "FROM workshop_tags wt JOIN tags t USING(tag_id) "
            "GROUP BY t.tag_name ORDER BY cnt DESC"
        )
    }


# --------------------------------------------------------------------------
# computation
# --------------------------------------------------------------------------


def compute(db_path: str, names: list[str] | None = None, params: dict | None = None) -> dict:
    """Run metrics and return ``{name: {"value": ..., "ms": ...}}``.

    Each metric is timed separately so the front ends can report what each one
    cost, and so a slow metric is blamed on itself rather than on the payload.
    One connection serves them all: opening a connection per metric would cost
    more than the whole instant tier.
    """
    wanted = list(names) if names is not None else all_names()
    unknown = [n for n in wanted if n not in REGISTRY]
    if unknown:
        raise KeyError(f"unknown metric(s): {', '.join(sorted(unknown))}")

    ctx = dict(params or {})
    conn = get_connection(db_path)
    try:
        results = {}
        for name in wanted:
            spec = REGISTRY[name]
            started = time.monotonic()
            try:
                value = spec.run(conn, ctx)
            except Exception as exc:
                # One broken metric must not take the whole screen down with it;
                # the front ends render each tier independently.
                logging.warning("[metrics] %s failed: %s", name, exc)
                value = None
            results[name] = {
                "value": value,
                "ms": round((time.monotonic() - started) * 1000, 1),
                "tier": spec.tier,
                "note": spec.note,
            }
        return results
    finally:
        conn.close()


def compute_tier(db_path: str, tier: str, params: dict | None = None) -> dict:
    """Run every metric in one tier."""
    return compute(db_path, names_in(tier), params)


def values(result: dict) -> dict:
    """Strip the timing wrapper, leaving ``{name: value}``."""
    return {name: entry["value"] for name, entry in result.items()}


if __name__ == "__main__":  # pragma: no cover - manual cost check
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "workshop.db"
    for _name, _r in compute(target).items():
        print(f"{_r['tier']:>7}  {_r['ms']:>8.1f} ms  {_name}")
