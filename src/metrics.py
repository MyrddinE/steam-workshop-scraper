"""Database statistics as named metrics, each an independent chunk.

The statistics are computed and delivered one metric at a time, so a front end
can draw each chunk the moment it is ready instead of waiting for the slowest.
Nothing here decides which metrics are "fast" or "slow".

`seed_ms` exists only to break the tie on the very first run, when nothing has
been measured yet. It is a hint, not a classification: it came from one database
on one machine, and a metric's real cost depends on the data — how many rows are
queued, how large the tag junction is, whether a suitable index exists. A front
end is expected to replace it with what the metric actually took last time it
ran, so the order follows the data rather than this table. If `tag_counts` gets
cheap, or `status_counts` gets expensive, the display reorders itself and no code
changes.

Two rules keep this honest:

* **A metric owns one question.** If two numbers are always wanted together and
  always cost the same, they are one metric; otherwise they are two.
* **Semantics do not change here.** This module returns exactly the statistics
  `get_db_stats` returned before it existed. Only how they are computed and
  delivered is different — moving the per-item classification into SQL changes
  the cost, not the answer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from src import db_poll
from src.database import get_connection

#: Used when a caller wants every metric and has no measurements of its own.
DEFAULT_STALENESS_DAYS = 30

#: One reporter for all metrics, keyed by metric name. The stats screen retries
#: a failed metric on its next scheduler tick; this keeps a persistent failure
#: from writing a warning every tick, while still reporting the first one.
_metric_failures = db_poll.RepeatFailureLog()


@dataclass(frozen=True)
class Metric:
    """One named statistic: how to compute it, and roughly what it costs."""

    name: str
    seed_ms: float
    note: str
    run: Callable[[Any, dict], Any]


REGISTRY: dict[str, Metric] = {}


def metric(name: str, seed_ms: float, note: str):
    """Register a function as a named metric.

    Every metric is called as ``fn(conn, params)``. Most ignore ``params``; it
    carries the few knobs that would otherwise have to be module globals.
    """
    def decorate(fn):
        if name in REGISTRY:
            raise ValueError(f"duplicate metric {name!r}")
        REGISTRY[name] = Metric(name=name, seed_ms=seed_ms, note=note, run=fn)
        return fn

    return decorate


def all_names() -> list[str]:
    """Every metric name, in the order `seed_ms` suggests running them.

    Cheapest-predicted first, so the first chunks to arrive are the ones most
    likely to arrive quickly. A caller with its own measurements should sort by
    those instead.
    """
    return sorted(REGISTRY, key=lambda name: (REGISTRY[name].seed_ms, name))


def catalogue() -> list[dict]:
    """What statistics exist, with the seed hint, for a front end to lay out."""
    return [
        {"name": name, "note": REGISTRY[name].note, "seed_ms": REGISTRY[name].seed_ms}
        for name in all_names()
    ]


def _resolve(names: list[str] | None) -> list[str]:
    wanted = list(names) if names is not None else all_names()
    unknown = [n for n in wanted if n not in REGISTRY]
    if unknown:
        raise KeyError(f"unknown metric(s): {', '.join(sorted(unknown))}")
    return wanted


def _run_one(conn, name: str, params: dict) -> dict:
    spec = REGISTRY[name]
    started = time.monotonic()
    try:
        value = spec.run(conn, params)
    except Exception as exc:
        # One broken metric must not take the whole screen down with it; the
        # front ends render each chunk independently. The report is throttled:
        # the stats screen re-runs a failed metric on its next scheduler tick,
        # so a persistent failure -- a database lock, most of all -- must not
        # write a line per tick forever. The first failure of a run warns and
        # repeats are debug until that metric succeeds again.
        _metric_failures.failed(name, "[metrics] %s failed: %s", name, exc)
        value = None
    else:
        _metric_failures.succeeded(name)
    return {
        "value": value,
        "ms": round((time.monotonic() - started) * 1000, 1),
        "note": spec.note,
        "seed_ms": spec.seed_ms,
    }


def iter_metrics(db_path: str, names: list[str] | None = None,
                 params: dict | None = None) -> Iterator[tuple[str, dict]]:
    """Yield ``(name, entry)`` as each metric finishes.

    One connection serves the whole run: opening one per metric would cost more
    than the fastest metrics do. This is a generator so a caller can render each
    chunk as it lands rather than holding everything until the last one is done.
    """
    ctx = dict(params or {})
    conn = get_connection(db_path)
    try:
        for name in _resolve(names):
            yield name, _run_one(conn, name, ctx)
    finally:
        conn.close()


def compute(db_path: str, names: list[str] | None = None, params: dict | None = None) -> dict:
    """Every requested metric at once, as ``{name: entry}``."""
    return dict(iter_metrics(db_path, names, params))


def values(result: dict) -> dict:
    """Strip the timing wrapper, leaving ``{name: value}``."""
    return {name: entry["value"] for name, entry in result.items()}


if __name__ == "__main__":  # pragma: no cover - manual cost check
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "workshop.db"
    total = 0.0
    for _name, _r in iter_metrics(target):
        total += _r["ms"]
        print(f"{_r['ms']:>8.1f} ms  {_name:<22} (seeded {_r['seed_ms']})")
    print(f"{total:>8.1f} ms  total")


# --------------------------------------------------------------------------
# cheap by nature: one row, or one small table
# --------------------------------------------------------------------------


@metric("high_water", 1, "The most recent successful API fetch.")
def _high_water(conn, params):
    return conn.execute("SELECT MAX(api_fetched_at) AS v FROM workshop_items").fetchone()["v"]


@metric("totals", 2, "Item counts, split by whether the item is still alive.")
def _totals(conn, params) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       COALESCE(SUM(CASE WHEN status = -1 THEN 1 ELSE 0 END), 0) AS dead "
        "FROM workshop_items"
    ).fetchone()
    total = row["total"] or 0
    dead = row["dead"] or 0
    return {"total": total, "dead": dead, "alive": total - dead}


@metric("app_tracking", 3, "Discovery position per application.")
def _app_tracking(conn, params) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT appid, last_page_scanned, last_cursor FROM app_tracking"
        )
    ]


# --------------------------------------------------------------------------
# per-queue completion: the three clocks migration 26->27 added
# --------------------------------------------------------------------------


def _completion_window(conn, column: str) -> dict:
    """A queue's completions in the two windows, and the newest one.

    ``last_success`` is None when the column holds no value at all. That is not
    a zero rate: those rows completed before the column existed, and a rate
    cannot be reconstructed from a time that was never written down. A front
    end renders that as "no history yet". Once a single stamp exists the counts
    are real answers -- 0 in the last hour then means an idle queue that was
    actually measured, which is a different statement.

    Each part is served by the partial index migration 26->27 adds
    (``WHERE <column> IS NOT NULL``): the range counts read only the window and
    ``MAX`` reads the newest entry, instead of scanning 2.6M rows for each of
    the three. ``MAX`` needs the explicit ``IS NOT NULL`` predicate to use a
    partial index.
    """
    now = int(time.time())
    row = conn.execute(
        f"""
        SELECT (SELECT COUNT(*) FROM workshop_items WHERE {column} >= ?) AS hour,
               (SELECT COUNT(*) FROM workshop_items WHERE {column} >= ?) AS day,
               (SELECT MAX({column}) FROM workshop_items
                 WHERE {column} IS NOT NULL) AS last_success
        """,
        (now - 3600, now - 86400),
    ).fetchone()
    if row["last_success"] is None:
        return {"hour": None, "day": None, "last_success": None}
    return {"hour": row["hour"], "day": row["day"], "last_success": row["last_success"]}


@metric("web_throughput", 2, "Web scrapes completed in the last hour and day, and the last success.")
def _web_throughput(conn, params) -> dict:
    return _completion_window(conn, "web_scraped_at")


@metric("image_throughput", 2, "Preview images fetched in the last hour and day, and the last success.")
def _image_throughput(conn, params) -> dict:
    return _completion_window(conn, "image_fetched_at")


@metric("translation_throughput", 2, "Items whose translation finished in the last hour and day, and the last success.")
def _translation_throughput(conn, params) -> dict:
    # One stamp per item, written when its last queued field is translated, so
    # this counts items completed, not fields.
    return _completion_window(conn, "translated_at")


# --------------------------------------------------------------------------
# one pass over workshop_items
# --------------------------------------------------------------------------


@metric("status_counts", 57, "Rows grouped by Steam's status code.")
def _status_counts(conn, params) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT status, COUNT(*) AS count FROM workshop_items GROUP BY status"
        )
    ]


@metric("stuck_work", 60, "Dead items still sitting in a work queue.")
def _stuck_work(conn, params) -> dict:
    """Work queued against items that are already known to be gone.

    An item marked dead should be in no queue, so this is expected to read zero.
    It is kept because the queues used to strand these rows: the daemon now
    clears the flags when it marks an item dead, and migration 16->17 cleared the
    rows already stranded. A non-zero reading means that cause has come back,
    which is more useful than a button that would hide the symptom.
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


@metric("fetch_recency", 79, "Our fetch recency, by last_fetch_attempted_at.")
def _fetch_recency(conn, params) -> dict:
    staleness_days = int(params.get("staleness_days", DEFAULT_STALENESS_DAYS))
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


@metric("coverage", 80, "How much of the live library each stage has reached.")
def _coverage(conn, params) -> dict:
    """Processing coverage over live items.

    This is the progress view: outstanding depth says how much is queued, but
    only coverage says how far along the library actually is. Dead items are
    excluded because they will never be covered, and counting them would make
    coverage fall as the library is cleaned up.

    The image stage counts a *recorded answer*, not only a stored file.
    ``image_extension`` holds the server's reply as well as a file type, so an
    item whose preview is permanently missing has been dealt with -- the
    question about it is settled -- and counting it as outstanding would leave
    the bar permanently short of the truth. What is still outstanding is an item
    with no answer at all.
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
    return {
        "total": row["total"] or 0,
        "api_fetched": row["api_fetched"],
        "described": row["described"],
        "imaged": row["imaged"],
        "translated": row["translated"],
        "attributed": row["attributed"],
    }


# --------------------------------------------------------------------------
# the expensive ones
# --------------------------------------------------------------------------

# `length(CAST(x AS BLOB))` counts UTF-8 bytes and `length(x)` counts characters,
# so they agree exactly when every character is single-byte -- that is, when the
# text is ASCII. Python's str.isascii() is the same test, and this reproduces it
# for control characters too, which a GLOB over printable ASCII would not.
_IS_ASCII = "length(CAST(COALESCE({c}, '') AS BLOB)) = length(COALESCE({c}, ''))"


@metric("translation_status", 255, "Translation state, classified in SQL rather than per row in Python.")
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


@metric("tag_counts", 358, "Tag frequencies from the junction table.")
def _tag_counts(conn, params) -> dict:
    return {
        row["tag_name"]: row["cnt"]
        for row in conn.execute(
            "SELECT t.tag_name, COUNT(*) AS cnt "
            "FROM workshop_tags wt JOIN tags t USING(tag_id) "
            "GROUP BY t.tag_name ORDER BY cnt DESC"
        )
    }


@metric("priority_breakdowns", 875, "Fetchable work per queue, by priority.")
def _priority_breakdowns(conn, params) -> dict:
    """Outstanding depth and priority mix, excluding items that cannot complete.

    Dead items used to be left flagged, so counting them here reported a backlog
    that no worker could ever drain. They are counted by `stuck_work` instead,
    where the number means what it says.
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
