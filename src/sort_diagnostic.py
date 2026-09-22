"""Read-only diagnostic for the search sort path.

The owner's report -- "I don't think subscriber score is indexed" -- cannot be
settled from this repository: both score columns are indexed and their plans are
identical here (see ``docs/search-filter.md``), so whatever makes the live file
slow is a property of *that* file or of the page path around the query. This
module turns the question into one read-only request the owner can run and paste
back, in the same spirit as the web-UI trace: ``GET /api/search_diagnostic``, and
the same summary logged once at web-server startup.

It is gated on ``daemon.capture_web_ui_trace`` through
:func:`src.capture.ui_trace_capture_active`, not a switch of its own: the
diagnostic is part of the same "make the web UI observable" instrument, and a
third debug key is one more thing to document and to remember to turn off.

The name of an index is not enough to answer the question. ``_ensure_indexes``
creates with ``CREATE INDEX IF NOT EXISTS``, so a live index that exists under the
expected name but on a different column -- or as a partial or unique index -- is
never repaired, and a name-only check reports it ``present``. ``_index_report``
therefore records each expected index's actual columns and ``unique``/``partial``
flags beside the expected ones and classifies it ``present``, ``missing`` or
``wrong_definition``; each sort column's plan carries a derived ``uses_index``
boolean beside the plan text, because "does the plan use the index" is the
question the owner is trying to answer by eye.

**Read-only.** The connection is opened with ``mode=ro`` and every statement is
a ``SELECT``, ``PRAGMA`` or ``EXPLAIN QUERY PLAN``. It must be safe to run
against the live file beside a running daemon, which is the only database whose
answer matters.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from src.database import QUERY_INDEXES, VALID_SORT_COLS, live_fetch_status_predicate

# One page, and the deep page the brief asked for. Both are named rather than
# inline so the log line and the route report say the same numbers.
PAGE_LIMIT = 50
DEEP_OFFSET = 50_000

SCORE_COLUMNS = ("wilson_favorite_score", "wilson_subscription_score")


def _open_read_only(db_path: str) -> sqlite3.Connection:
    """A ``mode=ro`` connection: no write, no journal-mode switch, no file creation."""
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _page_sql(sort_by: str, sort_order: str = "DESC") -> str:
    """The real summary query, mirrored from ``search_items(summary_only=True)``.

    The column list, the creators join, the live-status clause and the tag
    subquery are copied from that function deliberately: the point is to
    ``EXPLAIN`` and time the query the route actually runs, not a simplified
    stand-in whose plan could differ. It carries no filters -- the default page
    load is the case the owner reported.
    """
    order = "DESC" if sort_order.upper() == "DESC" else "ASC"
    columns = (
        "w.workshop_id, w.title, w.title_en, w.creator_steamid, w.consumer_appid, "
        "w.translate_version, w.is_queued_for_subscription, w.web_scrape_priority, "
        "w.image_priority, w.translation_priority, w.file_size, w.image_answer, "
        "w.wilson_subscription_score, w.wilson_favorite_score, w.fetch_status, "
        "w.own_subscribed, w.own_first_subscribed_at, w.steam_download_seen_at, "
        "u.personaname, u.personaname_en, "
        "(SELECT GROUP_CONCAT(t.tag_name, ', ') FROM workshop_tags wt "
        "JOIN tags t USING(tag_id) WHERE wt.workshop_id = w.workshop_id) as tags"
    )
    return (
        f"SELECT {columns} FROM workshop_items w "
        f"LEFT JOIN creators u ON w.creator_steamid = u.steamid "
        f"WHERE 1=1 AND {live_fetch_status_predicate('w.fetch_status')} "
        f"ORDER BY w.{sort_by} {order} LIMIT {PAGE_LIMIT} OFFSET ?"
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def run(db_path: str, sorts=None, deep_offset: int = DEEP_OFFSET) -> dict:
    """Build the diagnostic report. Never raises: a bad file becomes ``error``.

    ``sorts`` defaults to every ``VALID_SORT_COLS`` entry so a sortable column
    that later loses its index appears here without a code change.
    """
    sorts = sorted(sorts if sorts is not None else VALID_SORT_COLS)
    report: dict = {
        "database": str(db_path),
        "page": {"limit": PAGE_LIMIT, "deep_offset": deep_offset},
        "sorts": sorts,
    }
    try:
        conn = _open_read_only(db_path)
    except Exception as exc:  # missing file, permissions, a directory
        report["error"] = f"cannot open read-only: {exc}"
        return report

    try:
        if not _table_exists(conn, "workshop_items"):
            report["error"] = "workshop_items does not exist (is the database initialised?)"
            return report

        report["rows"] = conn.execute(
            "SELECT COUNT(*) FROM workshop_items").fetchone()[0]
        report["indexes"] = _index_report(conn)
        report["score_coverage"] = _coverage(conn, report["rows"])
        report["sqlite_stat1"] = _stat1(conn)
        report["plans"] = {
            column: _explain(conn, column) for column in sorts
        }
        report["timings"] = {
            column: _timings(conn, column, deep_offset) for column in sorts
        }
        return report
    except Exception as exc:
        report["error"] = f"diagnostic failed: {exc}"
        return report
    finally:
        conn.close()


def _expected_columns(columns: tuple[str, ...]) -> list[str]:
    """The column names ``QUERY_INDEXES`` expects, direction words stripped.

    A ``QUERY_INDEXES`` entry stores the fragment SQLite would take -- the poll
    index is ``("priority DESC", "queued_at ASC")`` -- while ``PRAGMA index_info``
    reports the bare name. The direction is not part of the definition comparison
    here: an index scanned in reverse serves the opposite single direction, and a
    mixed-direction index is still that index.
    """
    return [fragment.split()[0] for fragment in columns]


def _expected_index_for_sort(column: str) -> str | None:
    """The expected index whose *leading* column serves ``ORDER BY <column>``.

    ``None`` for a sortable column with no named index -- the rowid alias
    ``workshop_id``, whose B-tree *is* the table -- so the plan boolean is ``None``
    rather than a false alarm.
    """
    for name, table, columns in QUERY_INDEXES:
        if table == "workshop_items" and columns[0].split()[0] == column:
            return name
    return None


def _index_report(conn: sqlite3.Connection) -> dict:
    """Which indexes ``_ensure_indexes`` expects, and how the live definitions compare.

    For every ``QUERY_INDEXES`` entry: ``status`` is ``missing`` (the name is not in
    ``PRAGMA index_list``), ``wrong_definition`` (the name is there but its columns,
    ``unique`` or ``partial`` flag differ), or ``present``. A ``wrong_definition``
    entry carries both ``expected_columns`` and the live ``columns``, so the report
    names what was expected and what was found.
    """
    listed_by_table: dict[str, dict[str, dict]] = {}
    details: dict[str, dict] = {}
    missing: list[str] = []
    wrong_definition: list[str] = []
    for name, table, columns in QUERY_INDEXES:
        expected_columns = _expected_columns(columns)
        entry: dict = {"table": table, "expected_columns": expected_columns}
        listed = listed_by_table.get(table)
        if listed is None:
            # PRAGMA index_list: (seq, name, unique, origin, partial).
            listed = {
                row[1]: {"unique": bool(row[2]), "partial": bool(row[4])}
                for row in conn.execute(f"PRAGMA index_list({table})").fetchall()
            }
            listed_by_table[table] = listed
        if name not in listed:
            entry["status"] = "missing"
            missing.append(name)
        else:
            # PRAGMA index_info: (seqno, cid, name), name NULL for an expression.
            entry["columns"] = [
                row[2]
                for row in conn.execute(f"PRAGMA index_info({name})").fetchall()
            ]
            entry["unique"] = listed[name]["unique"]
            entry["partial"] = listed[name]["partial"]
            if (
                entry["columns"] != expected_columns
                or entry["unique"]
                or entry["partial"]
            ):
                entry["status"] = "wrong_definition"
                wrong_definition.append(name)
            else:
                entry["status"] = "present"
        details[name] = entry
    return {
        "expected": [name for name, _table, _columns in QUERY_INDEXES],
        "missing": missing,
        "wrong_definition": wrong_definition,
        "details": details,
    }


def _coverage(conn: sqlite3.Connection, rows: int) -> dict:
    """Non-NULL fraction of each score column, over the whole table."""
    coverage = {}
    for column in SCORE_COLUMNS:
        non_null = conn.execute(
            f"SELECT COUNT({column}) FROM workshop_items").fetchone()[0]
        coverage[column] = {
            "non_null": non_null,
            "rows": rows,
            "fraction": (non_null / rows) if rows else 0.0,
        }
    return coverage


def _stat1(conn: sqlite3.Connection) -> dict:
    """Whether ``sqlite_stat1`` exists, and its rows for the query indexes."""
    if not _table_exists(conn, "sqlite_stat1"):
        return {"present": False, "rows": 0, "indexes": {}}
    expected = {name for name, _table, _columns in QUERY_INDEXES}
    found = {
        row[0]: row[1]
        for row in conn.execute("SELECT idx, stat FROM sqlite_stat1").fetchall()
    }
    return {
        "present": True,
        "rows": len(found),
        "indexes": {name: found[name] for name in expected if name in found},
    }


def _explain(conn: sqlite3.Connection, column: str) -> dict:
    """The plan for ``column``'s query, plus whether it uses the expected index.

    ``uses_index`` is the derived boolean beside the plan text: it is true when the
    expected index's name appears in the plan. It is ``None`` for a sort column with
    no named index (the ``workshop_id`` rowid alias), where there is nothing to look
    for and a ``False`` would be a false alarm rather than a finding.
    """
    plan = " | ".join(
        row[3]
        for row in conn.execute(
            "EXPLAIN QUERY PLAN " + _page_sql(column), (0,)
        ).fetchall()
    )
    expected = _expected_index_for_sort(column)
    return {
        "expected_index": expected,
        "uses_index": None if expected is None else expected in plan,
        "plan": plan,
    }


def _timings(conn: sqlite3.Connection, column: str, deep_offset: int) -> dict:
    """Wall-clock seconds for the first page and one deep page.

    A warm-up run first, then one timed run per offset: this is a diagnostic,
    not a benchmark, and a repeated-run minimum would multiply the cost of the
    route the owner is waiting on.
    """
    sql = _page_sql(column)
    conn.execute(sql, (0,)).fetchall()
    result = {}
    for label, offset in (("first_page_seconds", 0), ("deep_page_seconds", deep_offset)):
        started = time.perf_counter()
        conn.execute(sql, (offset,)).fetchall()
        result[label] = round(time.perf_counter() - started, 4)
    return result


def format_summary(report: dict) -> list[str]:
    """The startup log's readable view: one line, plus a warning per problem."""
    if report.get("error"):
        return [f"[Sort diagnostic] could not run: {report['error']}"]

    lines = [
        "[Sort diagnostic] rows={rows} missing_indexes={missing} "
        "wrong_definition_indexes={wrong} sqlite_stat1={stat1}".format(
            rows=report.get("rows"),
            missing=report["indexes"]["missing"] or "none",
            wrong=report["indexes"]["wrong_definition"] or "none",
            stat1="present" if report["sqlite_stat1"]["present"] else "absent",
        )
    ]
    for column in SCORE_COLUMNS:
        coverage = report["score_coverage"].get(column, {})
        timing = report["timings"].get(column, {})
        plan = report["plans"].get(column, {})
        lines.append(
            "[Sort diagnostic] {column}: uses_index={uses} plan={plan!r} coverage={fraction:.4f} "
            "first_page={first:.3f}s deep_page={deep:.3f}s".format(
                column=column,
                uses=plan.get("uses_index"),
                plan=plan.get("plan", ""),
                fraction=coverage.get("fraction", 0.0),
                first=timing.get("first_page_seconds", 0.0),
                deep=timing.get("deep_page_seconds", 0.0),
            )
        )
    return lines


def log_startup_report(db_path: str) -> dict:
    """Run the diagnostic once at startup and log it.

    Missing indexes, indexes whose definition differs from the expected one, and
    any plan that falls back to a temp B-tree are logged at WARNING, loudly, so a
    live file that is missing one says so on the next start instead of silently
    proceeding.
    """
    report = run(db_path)
    for line in format_summary(report):
        logging.info("%s", line)
    if report.get("error"):
        logging.warning("[Sort diagnostic] %s", report["error"])
        return report
    if report["indexes"]["missing"]:
        logging.warning(
            "[Sort diagnostic] missing query indexes: %s",
            ", ".join(report["indexes"]["missing"]),
        )
    for name in report["indexes"]["wrong_definition"]:
        entry = report["indexes"]["details"][name]
        logging.warning(
            "[Sort diagnostic] index %s is defined on the wrong shape: "
            "expected columns %s, found %s (unique=%s partial=%s)",
            name,
            entry["expected_columns"],
            entry.get("columns"),
            entry.get("unique"),
            entry.get("partial"),
        )
    for column, plan in report["plans"].items():
        if "TEMP B-TREE" in plan["plan"].upper():
            logging.warning(
                "[Sort diagnostic] sort by %s does not use an index: %s",
                column, plan["plan"],
            )
    return report
