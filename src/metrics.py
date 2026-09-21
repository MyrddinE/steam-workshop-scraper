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

import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from src import activity
from src import db_poll
from src.database import (
    DEAD_FETCH_STATUS,
    IGNORED_FETCH_STATUS,
    api_fetch_queue_predicate,
    build_filters_sql,
    get_enrichment_filters,
    get_connection,
    image_queue_predicate,
    live_fetch_status_predicate,
    queued_anywhere_predicate,
    settled_fetch_status_predicate,
    translation_priority_predicate,
    web_scrape_queue_predicate,
)

#: Used when a caller wants every metric and has no measurements of its own.
DEFAULT_STALENESS_DAYS = 30

#: What the ``fetch_recency`` counts are, in one sentence both front ends print.
#: Defined here rather than written once in the TUI and retyped in the template,
#: so the two panels cannot describe the same figure differently -- the same
#: reason ``pending.TRANSLATION_REQUESTED_NOTICE`` exists.
FETCH_RECENCY_MEANING = (
    "Our last fetch attempt, not a backlog: it includes settled rows "
    "(dead and legacy 404s), and a stale row may not be due yet at the "
    "configured threshold."
)

#: Why a dead item still holding a queue flag matters, in one sentence both
#: front ends print. It deliberately does *not* say "the queues will not drain":
#: the API fetch poll excludes dead rows (``api_fetch_queue_predicate``), so
#: that queue still drains. Only the web, image and translation polls select on
#: their flag alone with no dead-item guard, so only they would keep spending
#: requests on a page that no longer exists. Defined here rather than written
#: once in the TUI and retyped in the template, so the two panels cannot
#: describe the same figure differently -- the same reason
#: ``FETCH_RECENCY_MEANING`` exists.
DEAD_QUEUED_MEANING = (
    "These rows can never complete. The API fetch poll excludes dead rows, so "
    "its queue still drains; the web, image and translation polls select on "
    "their flag alone and would keep spending requests on a page that no "
    "longer exists."
)


def item_staleness_days(daemon_config: dict | None) -> int:
    """The item re-fetch window, read from the one key that configures it.

    ``daemon.item_staleness_days`` is the window ``_promote_stale_items``
    promotes at, so it must also be the window ``fetch_recency`` buckets by, or
    the statistics would label rows stale that the sweep does not consider due.
    The daemon's sweep and both statistics front ends read the key through this
    function, so there is one default (the daemon's own 30 days) and one answer
    rather than each caller deriving its own.
    """
    return int((daemon_config or {}).get("item_staleness_days")
               or DEFAULT_STALENESS_DAYS)

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


def _resolve_names(names: list[str] | None) -> list[str]:
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
    metric_context = dict(params or {})
    # The runner knows the database path, and a metric that reads the
    # restart-surviving state kept beside it (`queue_eta` and the pause record)
    # needs it. It travels in the params dict every metric already receives
    # rather than widening the ``(conn, params)`` signature for one caller.
    metric_context["db_path"] = db_path
    conn = get_connection(db_path)
    try:
        for name in _resolve_names(names):
            yield name, _run_one(conn, name, metric_context)
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


@metric("item_counts", 2, "Item counts: live, dead and ignored, with the overall total.")
def _item_counts(conn, params) -> dict:
    """The table split into live, dead and ignored rows, plus the total.

    ``alive`` is the *live* population the searchable library shows -- the
    predicate is :func:`live_fetch_status_predicate`, so both settled statuses,
    dead (``-1``) and ignored (``-2``), are excluded from it rather than only
    death being subtracted from the total. The two settled counts are reported
    beside it so the Totals panel accounts for every row exactly once:
    ``alive + dead + ignored == total``.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       COALESCE(SUM(CASE WHEN fetch_status = ? THEN 1 ELSE 0 END), 0) AS dead, "
        "       COALESCE(SUM(CASE WHEN fetch_status = ? THEN 1 ELSE 0 END), 0) AS ignored, "
        f"       COALESCE(SUM(CASE WHEN {live_fetch_status_predicate()} "
        "                    THEN 1 ELSE 0 END), 0) AS alive "
        "FROM workshop_items",
        (DEAD_FETCH_STATUS, IGNORED_FETCH_STATUS),
    ).fetchone()
    return {
        "total": row["total"] or 0,
        "dead": row["dead"] or 0,
        "ignored": row["ignored"] or 0,
        "alive": row["alive"] or 0,
    }


@metric("app_discovery", 3, "Discovery position per application.")
def _app_discovery(conn, params) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT appid, last_cursor FROM app_discovery"
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
        SELECT (SELECT COUNT(*) FROM workshop_items WHERE {column} >= ?) AS last_hour,
               (SELECT COUNT(*) FROM workshop_items WHERE {column} >= ?) AS last_day,
               (SELECT MAX({column}) FROM workshop_items
                 WHERE {column} IS NOT NULL) AS last_success
        """,
        (now - 3600, now - 86400),
    ).fetchone()
    if row["last_success"] is None:
        return {"last_hour": None, "last_day": None, "last_success": None}
    return {"last_hour": row["last_hour"], "last_day": row["last_day"], "last_success": row["last_success"]}


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
# time to drain, per queue
# --------------------------------------------------------------------------

#: How long a drain rate is measured over. A day smooths an hourly rate enough
#: to be worth showing while still moving when the pace does.
DRAIN_WINDOW_SECONDS = 86400

#: name, completion column, outstanding predicate, whether `.pauselock` stops
#: this stage, and whether the queue's rate is net of the staleness sweep.
#:
#: The pause flag is per queue because the pause is: the daemon's web and image
#: workers poll `.pauselock` and genuinely stop, while the API fetch loop and the
#: translator are not gated by it (see `src/daemon.py`, `src/web_worker.py`,
#: `src/image_worker.py`). Subtracting the pause from a queue that kept working
#: would overstate its rate, so only the gated queues lose the paused time.
_DRAIN_QUEUES = (
    ("api", "api_fetched_at", api_fetch_queue_predicate(), False, True),
    ("web", "web_scraped_at", web_scrape_queue_predicate(), True, False),
    ("image", "image_fetched_at", image_queue_predicate(), True, False),
    ("translation", "translated_at", translation_priority_predicate(), False, False),
)

_LIVE_ITEM_UNALIASED = live_fetch_status_predicate()


def _drain_estimate(outstanding: int, completed: int, active_seconds: float) -> dict:
    """One queue's rate and time to drain, with a relative uncertainty.

    The completions in the window are modelled as a Poisson count: the rate is
    ``completed / active_seconds`` and the relative standard error of that count
    is ``1/sqrt(completed)``, so the rate and therefore the ETA carry a
    percentage uncertainty that is **wide while the evidence is thin and narrows
    as it accumulates** -- 100% at one completion, 50% at four, 10% at a hundred.
    That is the spread of the observed completions expressed as a count; the
    alternative, the spread of the inter-completion gaps, would need every
    timestamp in the window loaded instead of one indexed count.

    A queue with **no completions in the window gets no rate**: ``per_hour``,
    ``per_day``, ``eta_seconds`` and ``uncertainty_pct`` are all ``None``, which
    the front ends render as "no rate yet" beside the outstanding depth. That is
    the honest answer rather than a fabricated one -- a queue that completed
    nothing may be stalled, or may simply have had no work, and the volume of
    completions cannot tell those apart. An empty queue is not that case: with
    nothing outstanding the time to drain is a real zero.
    """
    estimate = {
        "outstanding": int(outstanding),
        "completed": int(completed),
        "active_seconds": int(round(active_seconds)),
    }
    if outstanding <= 0:
        estimate.update({"per_hour": 0.0, "per_day": 0.0,
                         "eta_seconds": 0.0, "uncertainty_pct": None})
        return estimate
    if completed <= 0 or active_seconds <= 0:
        estimate.update({"per_hour": None, "per_day": None,
                         "eta_seconds": None, "uncertainty_pct": None})
        return estimate
    rate = completed / active_seconds
    estimate.update({
        "per_hour": round(rate * 3600, 3),
        "per_day": round(rate * 86400, 2),
        "eta_seconds": round(outstanding / rate, 1),
        "uncertainty_pct": round(100.0 / math.sqrt(completed), 1),
    })
    return estimate


@metric("queue_eta", 150, "Outstanding depth, active-time rate and time to drain per queue, with uncertainty.")
def _queue_eta(conn, params) -> dict:
    """How long each of the four work queues will take to drain, at its own rate.

    One question per queue -- outstanding depth, the rate it has been draining
    at, and the time to drain -- measured over ``drain_window_seconds`` (a day by
    default). It is computed from whatever history exists, so it is shown
    immediately after the completion clocks start recording rather than waiting
    for a "stable" rate; the uncertainty the queue entry carries is what makes
    that safe to show, since it is a percentage and is widest while the evidence
    is thinnest.

    **The rate is in active time, not wall-clock**, so a pause does not read as a
    slowdown: the paused intervals are recorded beside the database
    (``src/activity.py``) and subtracted from the window for the queues `.pauselock`
    actually stops (web and image), with a pause still in progress counted up to
    now. The **API queue's completions are net** of the staleness sweep, whose
    own rowcount is recorded per run and subtracted when it falls inside the
    window. The other three queues' figures are **gross**: their inflow is the
    items an API refresh re-flags, which nothing records on our clock, so they
    are not a time to empty. Discovery's inflow into the API queue is likewise
    not counted -- `first_seen_at` is our clock but is not indexed, and a window
    count over it would be a full scan; the docs say so rather than implying it
    was measured (see `docs/data-pipeline.md`).

    The outstanding depth excludes dead items, matching `priority_breakdowns`:
    a dead item can never complete, so leaving it in would promise a drain that
    cannot happen. Completion counts are not filtered by liveness -- a completed
    item is a completion however that item ended.
    """
    db_path = params.get("db_path")
    window = int(params.get("drain_window_seconds") or DRAIN_WINDOW_SECONDS)
    if window <= 0:
        window = DRAIN_WINDOW_SECONDS
    now = int(time.time())
    window_start = now - window
    sweep = activity.sweep_inflow(db_path, window_start, now)
    paused = activity.paused_seconds(db_path, window_start, now)
    active = max(0.0, window - paused)

    queues: dict[str, dict] = {}
    for name, column, predicate, honours_pause, net in _DRAIN_QUEUES:
        gross = conn.execute(
            f"SELECT COUNT(*) AS n FROM workshop_items WHERE {column} >= ?",
            (window_start,),
        ).fetchone()["n"]
        subtracted = sweep if net else 0
        completed = max(0, gross - subtracted)
        outstanding = conn.execute(
            f"SELECT COUNT(*) AS n FROM workshop_items "
            f"WHERE ({predicate}) AND {_LIVE_ITEM_UNALIASED}"
        ).fetchone()["n"]
        entry = _drain_estimate(outstanding, completed,
                                active if honours_pause else float(window))
        entry["gross_completed"] = int(gross)
        entry["inflow_subtracted"] = int(subtracted)
        entry["basis"] = "net" if net else "gross"
        entry["honours_pause"] = bool(honours_pause)
        queues[name] = entry

    return {
        "window_seconds": window,
        "paused_seconds": int(round(paused)),
        "sweep_inflow": int(sweep),
        "queues": queues,
    }


# --------------------------------------------------------------------------
# one pass over workshop_items
# --------------------------------------------------------------------------


@metric("status_counts", 57, "Rows grouped by Steam's status code.")
def _status_counts(conn, params) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT fetch_status, COUNT(*) AS count FROM workshop_items GROUP BY fetch_status"
        )
    ]


@metric("dead_items_by_queue", 60, "Settled (dead or ignored) items still sitting in a work queue.")
def _dead_items_by_queue(conn, params) -> dict:
    """Work queued against items that are already settled.

    **The name is historical.** The population is every settled item --
    ``fetch_status`` in :data:`SETTLED_FETCH_STATUSES`, that is dead (``-1``) or
    ignored (``-2``). An ignored item holding a queue flag is the same violation
    as a dead one: the owner's marker settles the item exactly as death does, so
    it belongs to no queue. The name is kept because the metric is consumed by
    name in both front ends and renaming it is a separate concern.

    A settled item should be in no queue, so this is expected to read zero.
    It is kept because the queues used to strand these rows, in two shapes. The
    first is a queue flag: the daemon used to clear only `api_priority` when it
    marked an item dead, and migration 16->17 cleared the `web_scrape_priority`,
    `image_priority` and `translation_priority` flags already stranded. The
    second is a `translation_queue` row with its mirror cleared: the daemon did
    not delete those rows until issue 66, and migration 35->36 removed the ones
    already there. A non-zero reading means one of those causes has come back,
    which is more useful than a button that would hide the symptom.

    This is the other resolution of the question `dead_queued` answers: that metric
    is the scalar that must read zero, and this one breaks the same population
    down so the reading says which queue still holds the settled rows. The
    translation column therefore counts the mirror *or* a `translation_queue` row,
    exactly as the scalar's union does -- a settled item held only by a row has no
    flag column to name it, and a breakdown that read flags alone would put the
    item in the scalar and leave this diagnostic reading zero, which is the
    disagreement the breakdown exists to prevent. They are one question at two
    resolutions, not two findings.
    """
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(CASE WHEN web_scrape_priority > 0 THEN 1 ELSE 0 END), 0) AS web,
               COALESCE(SUM(CASE WHEN image_priority > 0 THEN 1 ELSE 0 END), 0) AS image,
               COALESCE(SUM(CASE
                              WHEN translation_priority > 0
                                OR EXISTS (SELECT 1 FROM translation_queue q
                                           WHERE q.entity_type = 'item'
                                             AND q.entity_id = workshop_items.workshop_id)
                              THEN 1 ELSE 0 END), 0) AS translation,
               COALESCE(SUM(CASE WHEN api_priority > 0 THEN 1 ELSE 0 END), 0) AS api
        FROM workshop_items
        WHERE {settled_fetch_status_predicate()}
        """
    ).fetchone()
    return {k: row[k] for k in ("web", "image", "translation", "api")}


@metric("dead_queued", 58, "Settled (dead or ignored) items a work queue would still select.")
def _dead_queued(conn, params) -> int:
    """Settled items a work queue would still select -- the shape of issues 17 and 66.

    **The name is historical.** The population is every settled item --
    ``fetch_status`` in :data:`SETTLED_FETCH_STATUSES`, dead (``-1``) or ignored
    (``-2``). An ignored item a queue would still select is the same violation as
    a dead one: both markers settle the item, both clear the four flags and the
    translation rows, and both must leave the item in no queue. The name is kept
    because the metric is consumed by name in both front ends.

    The handoff invariant is that every item is in exactly one state: queued for
    the API fetch (``api_priority > 0``), a web scrape (``web_scrape_priority > 0``),
    an image (``image_priority > 0``) or a translation (a ``translation_queue`` row
    exists); complete for the stage that owns it; or deliberately settled
    (``fetch_status`` in ``-1``/``-2``) and therefore in **no** queue.

    A settled item a queue would still select is the second kind of violation: the
    stage that settled it wrote the status but left work behind. It
    can be a flag -- the web, image and translation polls each select on their flag
    alone, with no settled-item guard -- or, for translation, the queue row itself,
    which is the consumer's real predicate and outlives a cleared mirror.

    Zero is the healthy reading. A non-zero value is the number of settled items
    still encumbered by a queue, each item counted once however many flags it
    holds. `dead_items_by_queue` answers the same question at the other
    resolution: this is the scalar that must read zero, and that metric is the
    per-queue breakdown that says where the item is held -- a flag, or for
    translation a queue row. Both are wanted -- the scalar is the invariant, the
    breakdown is the diagnosis -- so one is not a replacement for the other, and
    the two must agree.
    """
    # The union comes from the named queue predicates, so a change to one of them
    # moves this with it. ``api_priority`` is then added on its own, because the
    # API fetch predicate guards on fetch_status -- it reads ``api_priority > 0 AND
    # live_fetch_status_predicate()`` -- and this metric's population is
    # ``settled_fetch_status_predicate()`` by definition, so that guarded term can
    # never be true here. Without the extra term a settled row whose only leftover
    # flag is the fetch priority would stop being counted, and it is the same
    # violation: `_settle_api_failure` and `ignore_item` clear all four flags.
    #
    # Measured against the 2026-09-18 backup, before the queue-row term below
    # existed: the hand-written union and this one both counted 4, all 4 dead
    # rows holding only ``api_priority > 0``, while the bare named union counted 0.
    # The named union now also asks the translation queue, which only ever adds
    # to that count.
    return conn.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM workshop_items
        WHERE {settled_fetch_status_predicate()}
          AND (({queued_anywhere_predicate()}) OR api_priority > 0)
        """
    ).fetchone()["n"]


@metric("queued_nowhere", 63, "Live items no queue is carrying and the pipeline has not finished.")
def _queued_nowhere(conn, params) -> int:
    """Items in no queue that the pipeline never completed -- issues 19 and 20.

    The handoff invariant is that every item is in exactly one state: queued for
    the API fetch (``api_priority > 0``), a web scrape (``web_scrape_priority > 0``),
    an image (``image_priority > 0``) or a translation (a ``translation_queue`` row
    exists); complete for the stage that owns it; or deliberately settled
    (``fetch_status`` in ``-1``/``-2``) in no queue. This counts the first kind of
    violation -- an item that fell out of the pipeline without being finished:

    * discovered but never fetched (``fetch_status IS NULL``) with no fetch priority,
      which is issue 20; or
    * fetched (``fetch_status = 200``) with no stored description and no scrape queued,
      which is issue 19.

    Zero is the healthy reading: every live item is queued somewhere or has been
    carried through to a stored description. A non-zero value is the number of
    live items no stage is carrying. Some of those can be item pages that were
    served without an extended description, which the web stage deliberately
    settles and which look identical in the database to issue 19's stranded rows,
    so the number is a population to inspect rather than proof of a live defect.
    Statuses the pipeline settles otherwise -- dead (``-1``) and the legacy
    ``404`` rows of issue 36 -- are outside the population.
    """
    # Stated as the negation of the same union, so the two halves of the handoff
    # invariant cannot drift apart. Inside this population the API predicate's
    # fetch_status guard is always true -- an item here is either ``fetch_status IS NULL`` or
    # ``fetch_status = 200``, never dead -- so before the translation-queue term
    # was added the negation said exactly what the four hand-written ``<= 0``
    # terms said. Verified equal on the 2026-09-18 backup: both forms counted 3456.
    # The translation-queue term now also keeps an item with outstanding
    # translation work out of this population, which is the correct reading of
    # the invariant (issue 66).
    return conn.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM workshop_items
        WHERE (fetch_status IS NULL
               OR (fetch_status = 200 AND COALESCE(extended_description, '') = ''))
          AND NOT ({queued_anywhere_predicate()})
        """
    ).fetchone()["n"]


@metric(
    "fetch_recency",
    79,
    "Age of our last fetch attempt per row (last_fetch_attempted_at), at "
    "daemon.item_staleness_days. Not the fetch queue: settled rows (dead and "
    "legacy 404s) are counted, and a stale row is only a re-fetch candidate.",
)
def _fetch_recency(conn, params) -> dict:
    """Row counts by the age of **our last fetch attempt**, not of Steam's data.

    The window is ``staleness_days`` when the caller supplies it -- both front
    ends pass ``daemon.item_staleness_days`` through :func:`item_staleness_days`,
    so the boundary is the one ``_promote_stale_items`` promotes at -- and
    ``DEFAULT_STALENESS_DAYS`` otherwise. The window used travels back in the
    value as ``window_days`` so the label has a single owner and the two panels
    cannot disagree with the query that produced the counts.

    Read the counts as attempt recency, not as work outstanding:

    * it measures ``last_fetch_attempted_at``, which a *failed* attempt also
      stamps, so it is not the clock the API queue's time-to-drain uses;
    * it counts every row, so dead items (``fetch_status = -1``) and the legacy
      ``404`` rows are inside ``fresh``/``stale`` even though neither will ever
      be fetched again;
    * a ``stale`` row is only older than the window. The sweep's criterion is
      narrower -- live, successful, unqueued, and ``api_fetched_at`` older than
      the same window -- and it runs hourly, so the stale band is a candidate
      list, not the queue, and a stale row may simply not be due yet.
    """
    staleness_days = int(params.get("staleness_days", DEFAULT_STALENESS_DAYS))
    threshold = int(time.time()) - staleness_days * 86400
    counts = {"fresh": 0, "stale": 0, "unknown": 0, "window_days": staleness_days}
    rows = conn.execute(
        """
        SELECT CASE
                 WHEN typeof(last_fetch_attempted_at) NOT IN ('integer', 'real') THEN 'unknown'
                 WHEN last_fetch_attempted_at IS NULL OR last_fetch_attempted_at = 0 THEN 'unknown'
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
# coverage, at two scopes
# --------------------------------------------------------------------------

#: What a bar whose reachable population is empty says. A zero population is a
#: legitimate answer -- there is nothing to translate -- and must not be drawn as
#: a permanent 0.0%.
NOTHING_TO_TRANSLATE = "Nothing to translate"

#: Live items only: settled items -- dead (``-1``) and ignored (``-2``) -- can
#: never be covered.
_LIVE_ITEM_ALIASED = live_fetch_status_predicate("w.fetch_status")


def _ascii_sql(column: str) -> str:
    """SQL for Python's ``str.isascii()`` on a column, NULL/empty reading ASCII.

    The same UTF-8-bytes-equals-characters test ``_ASCII_SQL_TEMPLATE`` uses: the two
    lengths agree exactly when every character is single-byte, control
    characters included. ``COALESCE`` makes NULL and '' ASCII, which is what
    ``is_ascii`` does with a falsy value, so the flagging rule and this
    translation of it agree on the empty field too.
    """
    return (f"length(CAST(COALESCE({column}, '') AS BLOB)) "
            f"= length(COALESCE({column}, ''))")


def _field_current_sql(en_column: str) -> str:
    """SQL for :func:`translation_is_current` on one item field.

    ``translation_is_current(translated, translate_version, steam_updated_at)``
    is written out here for the same reason the scoped figure is written out
    elsewhere: the metric must compare the stored translation against the item's
    current Steam revision exactly as the flagging path does, or a bar and the
    work it measures could disagree. A NULL ``steam_updated_at`` means no change
    can be detected, so a stored translation counts as current; a NULL
    ``translate_version`` is unknown provenance and does not.
    """
    return (f"COALESCE(w.{en_column}, '') <> '' AND ("
            f"w.steam_updated_at IS NULL OR ("
            f"w.translate_version IS NOT NULL "
            f"AND w.translate_version >= w.steam_updated_at))")


def _creator_current_sql() -> str:
    """SQL for :func:`translation_is_current` on a creator's name.

    The name lives on ``creators`` and has no Steam revision, so the pair of clocks
    that decide currency are both ours: ``translated_at`` (stamped when the name
    was translated) against ``api_fetched_at`` (stamped when the persona was
    last fetched). With no fetch time the stored name cannot be stale, so a
    stored translation is current -- the same NULL rule the item fields use.
    """
    return ("COALESCE(u.personaname_en, '') <> '' AND ("
            "u.api_fetched_at IS NULL OR ("
            "u.translated_at IS NOT NULL "
            "AND u.translated_at >= u.api_fetched_at))")


def _coverage_scan(conn, where_sql: str, params: list) -> dict:
    """Every count the bars need, from one pass over the scope's live items.

    The population tests here are the flagging rules, in SQL. A field is in a
    translation bar's population exactly when the code that queues it would
    queue it: non-empty and non-ASCII (``queue_field_for_translation`` returns
    early on an empty or ASCII field), and a stored translation counts only when
    :func:`translation_is_current` says it is current. The creator's name is the
    same test read through ``creators``, but it is counted in **authors**, not
    items: a persona lives once per creator and is shared by every item that
    creator made, so ``COUNT(DISTINCT w.creator_steamid)`` is the population the
    Creator rows report.

    ``blank_answers`` is the scrape's legitimate-blank ceiling: a page that was
    scraped and answered with an empty description can never make the Extended
    Web bar move, so it is subtracted from that bar's maximum. The one other
    settled-blank path -- a served item page whose description element is absent
    -- records no item column to count, so the ceiling counts the scraped-empty
    rows alone; see ``docs/data-pipeline.md``.
    """
    title = _ascii_sql("w.title")
    short = _ascii_sql("w.short_description")
    extended = _ascii_sql("w.extended_description")
    persona = _ascii_sql("u.personaname")
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN w.api_fetched_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS api_fetched,
               COALESCE(SUM(CASE WHEN COALESCE(w.extended_description, '') <> '' THEN 1 ELSE 0 END), 0) AS described,
               COALESCE(SUM(CASE WHEN w.web_scraped_at IS NOT NULL
                                  AND COALESCE(w.extended_description, '') = ''
                                 THEN 1 ELSE 0 END), 0) AS blank_answers,
               COALESCE(SUM(CASE WHEN COALESCE(w.image_answer, '') <> '' THEN 1 ELSE 0 END), 0) AS imaged,
               COUNT(DISTINCT CASE WHEN COALESCE(w.creator_steamid, '') <> ''
                                   THEN w.creator_steamid END) AS authors,
               COUNT(DISTINCT CASE WHEN u.steamid IS NOT NULL
                                   THEN w.creator_steamid END) AS authors_known,
               COALESCE(SUM(CASE WHEN COALESCE(w.title, '') <> '' AND NOT ({title})
                                 THEN 1 ELSE 0 END), 0) AS title_need,
               COALESCE(SUM(CASE WHEN COALESCE(w.title, '') <> '' AND NOT ({title})
                                  AND ({_field_current_sql('title_en')})
                                 THEN 1 ELSE 0 END), 0) AS title_done,
               COALESCE(SUM(CASE WHEN COALESCE(w.short_description, '') <> '' AND NOT ({short})
                                 THEN 1 ELSE 0 END), 0) AS short_need,
               COALESCE(SUM(CASE WHEN COALESCE(w.short_description, '') <> '' AND NOT ({short})
                                  AND ({_field_current_sql('short_description_en')})
                                 THEN 1 ELSE 0 END), 0) AS short_done,
               COALESCE(SUM(CASE WHEN COALESCE(w.extended_description, '') <> '' AND NOT ({extended})
                                 THEN 1 ELSE 0 END), 0) AS extended_need,
               COALESCE(SUM(CASE WHEN COALESCE(w.extended_description, '') <> '' AND NOT ({extended})
                                  AND ({_field_current_sql('extended_description_en')})
                                 THEN 1 ELSE 0 END), 0) AS extended_done,
               COUNT(DISTINCT CASE WHEN u.personaname IS NOT NULL AND NOT ({persona})
                                   THEN w.creator_steamid END) AS creator_need,
               COUNT(DISTINCT CASE WHEN u.personaname IS NOT NULL AND NOT ({persona})
                                    AND ({_creator_current_sql()})
                                   THEN w.creator_steamid END) AS creator_done
        FROM workshop_items w
        LEFT JOIN creators u ON w.creator_steamid = u.steamid
        WHERE {where_sql}
        """,
        params,
    ).fetchone()
    return {key: row[key] or 0 for key in row.keys()}


def _reachable_detail(total: int, maximum: int, explanation: str) -> str:
    """The counts behind a shortened bar: what it can reach, and why.

    Every bar is drawn on one width representing 100% of the scope's live items,
    so a bar whose reachable share is small is not a defect; this sentence is
    what says so. Its percentage is the reachable share, a different number from
    the fill percentage printed beside the bar (the share already done), and both
    are computed in this module so the two cannot come from different places.
    """
    if not total:
        return explanation
    return (f"reachable {maximum:,} of {total:,} ({maximum / total * 100:.1f}%): "
            f"{explanation}")


def _share_note(need: int, slots: int) -> str | None:
    """The short share of a translation bar's track that still needs work.

    A translation bar's track counts *slots* -- the fields the stage could
    translate -- so the one number worth printing under it is what fraction of
    those slots still needs a translation. It is written here, once, so the
    terminal and the browser print the identical string; a bar with nothing to
    translate returns ``None`` and the front end shows its ``empty`` wording
    instead of a note that would read "0% need translation".
    """
    if slots <= 0 or need <= 0:
        return None
    return f"{need / slots * 100:.0f}% need translation"


def _coverage_bar(key: str, label: str, subsidiary: bool, done: int, maximum: int,
                  total: int, detail: str | None, empty: str | None = None,
                  slot_units: bool = False) -> dict:
    """One bar, with everything a front end needs to draw and label it.

    ``pct`` is the bar's fill as a share of its own track, computed here once so
    the terminal and the browser cannot print different numbers. For most bars
    the track is the scope's live items, so that share is coverage of the
    library; for a translation bar ``total`` is the stage's **slots** instead --
    two per entry for the API fields, one per described item for the web
    description, one per author for the Creator name -- and the fill is the
    share of the work that is done.

    ``slot_units`` marks those translation bars: the track then has a third
    part, the gray share ``total - maximum`` that needs no translation at all.
    ``no_work`` and ``gray_pct`` are computed here, beside ``pct``, so both
    front ends draw the same two segments. ``pct`` is ``None`` when the
    reachable population is empty, which is the case a front end renders with
    ``empty`` instead of a stuck 0.0%.
    """
    bar = {
        "key": key,
        "label": label,
        "subsidiary": subsidiary,
        "done": int(done),
        "maximum": int(maximum),
        "total": int(total),
        "pct": None if maximum <= 0 or total <= 0 else round(done / total * 100, 1),
        "detail": detail,
        "empty": empty,
        "no_work": None,
        "gray_pct": None,
    }
    if slot_units and total > 0 and maximum > 0:
        no_work = max(0, int(total) - int(maximum))
        bar["no_work"] = no_work
        bar["gray_pct"] = round(no_work / total * 100, 1)
    return bar


def _enrichment_scope_predicate(conn, target_appids) -> tuple[str, list, dict]:
    """The SQL predicate for "what I care about": the target AppIDs' filters.

    Each target AppID contributes ``consumer_appid = ? AND <its filters>`` and
    the AppIDs are joined with OR, so the figure is the **union** of whatever any
    target's filters select. An AppID with no stored filters, or one whose stored
    set could not be read, contributes its AppID alone and therefore everything
    it owns -- the same contract as :func:`get_enrichment_filters` (``None`` and
    ``[]`` both mean "no exclusion").

    Returns ``(predicate, params, detail)``. ``predicate`` is empty when there is
    no target AppID at all, and the caller then counts the whole live library for
    both figures: with nothing to restrict to, "what I care about" is everything,
    exactly as an empty filter set is.

    ``detail`` names the AppIDs used, the ones with a readable non-empty filter
    set (``with_filters``), the subset of those whose filters actually produce a
    predicate (``restricting``) and the ones whose stored set was unreadable
    (``unreadable``), so a front end can explain why the two figures coincide
    rather than leaving it looking like a bug.
    """
    if target_appids is None:
        target_appids = [
            row["appid"]
            for row in conn.execute("SELECT appid FROM app_discovery ORDER BY appid")
            if row["appid"] is not None
        ]
    appids: list[int] = []
    for value in target_appids:
        try:
            appids.append(int(value))
        except (TypeError, ValueError):
            continue
    if not appids:
        return "", [], {"appids": [], "with_filters": [], "restricting": [],
                        "unreadable": []}

    clauses: list[str] = []
    params: list = []
    with_filters: list[int] = []
    restricting: list[int] = []
    unreadable: list[int] = []
    for appid in appids:
        row = conn.execute(
            "SELECT * FROM app_discovery WHERE appid = ?", (appid,)
        ).fetchone()
        tracking = dict(row) if row is not None else None
        filters = get_enrichment_filters(tracking) if tracking else []
        if tracking is not None and filters is None:
            unreadable.append(appid)
        parts = ["w.consumer_appid = ?"]
        app_params: list = [appid]
        if filters:
            with_filters.append(appid)
            group, group_params = build_filters_sql(filters)
            if group:
                restricting.append(appid)
                parts.append(f"({group})")
                app_params.extend(group_params)
        clauses.append("(" + " AND ".join(parts) + ")")
        params.extend(app_params)
    predicate = "(" + " OR ".join(clauses) + ")"
    return predicate, params, {
        "appids": appids,
        "with_filters": with_filters,
        "restricting": restricting,
        "unreadable": unreadable,
    }


def _coverage_bars(counts: dict, total: int, translations: dict) -> list[dict]:
    """The seven bars for one scope, in pipeline order.

    ``counts`` is the scope's scan and ``translations`` the filter-selected scan
    that feeds the Translations bar at both scopes (the flagging path only ever
    queues enriched items, so that bar's population does not follow the scope).

    Every bar's track is its own 100%. For the item-scale bars that is ``total``
    live items. The **Translation** bars are drawn in **slots** instead, because
    that is the unit the queue works in: two per entry for the API's title and
    short description, one per described item for the scraped description, and
    one per author for the Creator name. A bar's ``maximum`` is the count it can
    ever reach, its population, and the part of the slot track above it needs no
    translation at all -- the gray segment ``_coverage_bar`` computes.

    The two **Creator** bars are in **author** units, not items: a persona is
    shared by every item its creator made, so counting items under "Creator"
    printed roughly the item count where the population is the scope's unique
    authors.
    """
    blank = counts["blank_answers"]
    reachable_web = max(0, total - blank)
    translation_slots = 2 * total
    translation_need = translations["title_need"] + translations["short_need"]
    translation_done = translations["title_done"] + translations["short_done"]
    web_slots = counts["described"]
    return [
        _coverage_bar("api_fetched", "API Data", False,
                      counts["api_fetched"], total, total, None),
        _coverage_bar(
            "translations", "Translations", True,
            translation_done, translation_need, translation_slots,
            _share_note(translation_need, translation_slots),
            NOTHING_TO_TRANSLATE, slot_units=True),
        _coverage_bar(
            "described", "Extended Web", False,
            counts["described"], reachable_web, total,
            _reachable_detail(
                total, reachable_web,
                f"{blank:,} scraped pages answered with no description")),
        _coverage_bar(
            "web_translated", "Extended Web Translation", True,
            counts["extended_done"], counts["extended_need"], web_slots,
            _share_note(counts["extended_need"], web_slots),
            NOTHING_TO_TRANSLATE, slot_units=True),
        _coverage_bar("imaged", "Images", False,
                      counts["imaged"], total, total, None),
        _coverage_bar("attributed", "Creator", False,
                      counts["authors_known"], counts["authors"],
                      counts["authors"], None),
        _coverage_bar(
            "creator_translated", "Creator Translation", True,
            counts["creator_done"], counts["creator_need"], counts["authors"],
            _share_note(counts["creator_need"], counts["authors"]),
            NOTHING_TO_TRANSLATE, slot_units=True),
    ]


@metric("coverage", 80, "How much of the live library each stage has reached, at both scopes.")
def _coverage(conn, params) -> dict:
    """Processing coverage over live items, at two scopes, as seven bars.

    The first figure is the whole live library: this is the progress view, and
    outstanding depth says how much is queued while only coverage says how far
    along the library actually is. Dead items are excluded because they will
    never be covered, and counting them would make coverage fall as the library
    is cleaned up.

    The second figure, under ``filtered``, is the same bars restricted to *what
    the owner cares about*: the items the target AppIDs' ``enrichment_filters``
    select. Its population is the one the daemon calls *enriched*. Each AppID
    contributes ``consumer_appid = ? AND <its filters>`` and the AppIDs are ORed
    together, so with more than one target the figure is the **union** of what
    any target's filters select. The AppIDs come from the ``target_appids``
    parameter when a front end can supply the configured list, and otherwise
    from every row in ``app_discovery``.

    **The bars' populations are the flagging rules, in SQL**, so a bar and the
    work it measures cannot disagree. Each field is counted exactly when the code
    that queues it would queue it, and a stored translation counts only when
    :func:`translation_is_current` says it is current -- non-ASCII, non-empty,
    taken at the item's current Steam revision. The three translation bars have
    three different scopes, because the code that feeds them does:

    * **Translations** is per *field*, not per item, over ``title`` and
      ``short_description``. ``_queue_translations`` returns early unless the item
      was enriched, so its population is the non-ASCII API fields of the
      **filter-selected** items. It is the one population that does not follow
      the displayed scope: the same absolute figures appear in both blocks, only
      the track -- two slots per live item -- changes. It is drawn in those
      translation units, so its 100% is two translations per entry, with a gray
      segment for the fields that need no translation at all.
    * **Extended Web Translation** is over ``extended_description``. The scrape
      flags its description for translation regardless of enrichment, so its
      population is **any scraped item** with a non-ASCII description -- not only
      the filter-selected ones. It is drawn the same way, one slot per described
      item; its maximum is at most the Extended Web bar's, because a non-ASCII
      description is a description.
    * **Creator Translation** is over ``creators.personaname``. Only enriched items
      refresh a persona, but the name lives per creator and is shared by every item
      that creator made, so the bar counts **authors whose name is non-ASCII**,
      one slot per author in the scope.

    The two **Creator** bars are in author units for the same reason: the
    population under the "Creator" label is the scope's unique authors, not the
    items they made, so the fill is the share of authors the stage has reached.

    The **Extended Web** bar is the scrape's coverage, not one field's: live items
    with a non-empty ``extended_description``. Its maximum excludes the pages
    that legitimately carried no description -- a scrape that answered with an
    empty description can never move the bar -- so the ceiling is shown rather
    than a full-width track promising work that cannot exist. That count of
    scraped-but-blank pages stays on the parent's sentence, which is not a
    translation bar and keeps its explanation.

    A bar whose population is zero is not a divide by zero and not a stuck 0.0%:
    its ``pct`` is ``None`` and both front ends print ``NOTHING_TO_TRANSLATE``.
    A library with nothing to translate has nothing to translate, and the bar
    says so.

    **The scoped figure is a translation, not a re-derivation of the daemon's
    per-item decision.** It is built by :func:`src.database.build_filters_sql`,
    the same SQL builder a search uses, while the daemon's in-memory
    :func:`src.database._evaluate_filters` reads the original columns alone. The
    builder also searches each text field's ``_en`` counterpart, so the two can
    disagree on an item whose original text does not match but whose stored
    translation does. Where they disagree, this is the search builder's answer; the
    demotion walk deliberately keeps using the Python one. A ``percentile``
    filter has no fixed predicate and is skipped by both, since it is relative to
    the result set it is computed over. The same caveat the old filtered figure
    carried now applies to the Translations bar's population, whose rule
    (``_queue_translations``) is Python today and whose metric is this SQL, and to
    the Creator Translation bar's population, whose rule is the ``is_ascii`` test
    in the daemon's ``_store_user_record`` and whose metric is
    :func:`_creator_current_sql`.

    An unreadable or empty filter set means *everything* for that AppID
    (:func:`get_enrichment_filters`'s contract: ``None`` and ``[]`` both mean no
    exclusion), so the two figures then coincide. ``filtered.with_filters`` and
    ``filtered.unreadable`` say which AppIDs actually restricted anything, so the
    coincidence reads as the contract it is.

    The image stage counts a *recorded answer*, not only a stored file.
    ``image_answer`` holds the server's reply as well as a file type, so an
    item whose preview is permanently missing has been dealt with -- the
    question about it is settled -- and counting it as outstanding would leave
    the bar permanently short of the truth. What is still outstanding is an item
    with no answer at all.
    """
    overall_counts = _coverage_scan(conn, _LIVE_ITEM_ALIASED, [])
    predicate, predicate_params, detail = _enrichment_scope_predicate(
        conn, params.get("target_appids"))
    if predicate:
        filtered_counts = _coverage_scan(
            conn, f"{_LIVE_ITEM_ALIASED} AND {predicate}", list(predicate_params))
    else:
        filtered_counts = dict(overall_counts)

    total = overall_counts["total"]
    filtered_total = filtered_counts["total"]
    overall = {
        "total": total,
        "bars": _coverage_bars(overall_counts, total, filtered_counts),
    }
    filtered = {
        "total": filtered_total,
        "bars": _coverage_bars(filtered_counts, filtered_total, filtered_counts),
        **detail,
    }
    return {**overall, "filtered": filtered}


# --------------------------------------------------------------------------
# the expensive ones
# --------------------------------------------------------------------------

# `length(CAST(x AS BLOB))` counts UTF-8 bytes and `length(x)` counts characters,
# so they agree exactly when every character is single-byte -- that is, when the
# text is ASCII. Python's str.isascii() is the same test, and this reproduces it
# for control characters too, which a GLOB over printable ASCII would not.
_ASCII_SQL_TEMPLATE = "length(CAST(COALESCE({c}, '') AS BLOB)) = length(COALESCE({c}, ''))"


@metric("translation_status", 255, "Translation state, classified in SQL rather than per row in Python.")
def _translation_status(conn, params) -> dict:
    all_ascii = " AND ".join(
        _ASCII_SQL_TEMPLATE.format(c=c)
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
    that no worker could ever drain. They are counted by `dead_items_by_queue` instead,
    where the number means what it says.
    """
    out = {}
    for column in ("translation_priority", "image_priority", "web_scrape_priority"):
        out[column] = [
            dict(r)
            for r in conn.execute(
                f"SELECT {column} AS prio, COUNT(*) AS cnt FROM workshop_items "
                f"WHERE {column} > 0 AND {live_fetch_status_predicate()} "
                f"GROUP BY {column} ORDER BY prio DESC"
            )
        ]
    return out
