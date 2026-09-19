"""Embedded web server for Steam Workshop Scraper."""

import json
import os
import time
import re
import logging
from flask import Flask, request, jsonify, render_template, send_from_directory
from src.database import search_items, get_item_details, get_db_stats, get_all_creator_ids, save_enrichment_filters, compute_wilson_cutoffs, raise_web_scrape_priority_for_list, raise_web_scrape_priority_for_detail, raise_translation_priority_for_list, raise_translation_priority_for_detail, raise_image_priority_for_list, raise_image_priority_for_detail, raise_image_priority, get_connection, toggle_subscription_queue, clear_subscription_queue, mark_own_subscribed, get_subscription_queue_items, SEARCH_FILTER_SCHEMA, raise_api_priority_for_detail, delete_never_fetched_items
from src.analysis import view_window_analysis
from src import capture
from src import crash
from src import activity
from src import images
from src import metrics
from src import session_health
from src import subscribe_engine
from src import subscription
from src import web_scraper
from src import workshop_folders
from src.config import login_secure_value, save_config
from src.daemon_control import DaemonController
from src.firefox_cookies import steam_login_secure
from src.web_worker import WEB_DELAY_DEFAULT

app = Flask(__name__, template_folder='../templates')
app.config['TEMPLATES_AUTO_RELOAD'] = True


def _attach_image_state(rows):
    """Attach the image classification the grid branches on.

    Computed here from `src/images.py` rather than left to the browser: if the
    page re-derived the rule from the raw column it would need its own copy of
    the extension allowlist, and the writer and the reader disagreeing about
    what counts as a picture is the bug this exists to prevent.
    """
    for row in rows:
        stored = row.get("image_answer")
        row["image_state"] = images.image_state(stored)
        # Sent alongside so the page never has to know which states count as
        # settled; only src/images.py decides that, and this calls its predicate
        # rather than spelling the rule out a second time here.
        row["image_resolved"] = images.is_resolved(stored)
    return rows


@app.after_request
def _do_not_cache_generated_pages(response):
    """Stop the browser serving a stale copy of the page or the userscript.

    Both are generated per request and read from disk each time, so a cached copy
    is always a stale copy — and there were no validators either, no ETag and no
    Last-Modified, so the browser had nothing to revalidate against and reused
    its copy freely. That cost real time: a web-UI fix was deployed and verified
    on the server while the browser kept running the previous page, which made
    the fix look broken.

    Scoped away from /images: those are large, immutable once written, and worth
    caching.
    """
    if not request.path.startswith('/images/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


_db_path = "workshop.db"
_config = {}
_images_dir = "images"
_pushed_sessionid = ""
_config_path = "config.yaml"
_daemon_controller = None
# The shared folder helper. Created by init_webserver; None until then, which is
# what makes the open-folder route refuse (the feature is off) rather than
# raising on an uninitialised server.
_workshop_folders = None


def init_webserver(db_path: str, config: dict, config_path: str = "config.yaml",
                   daemon_controller: DaemonController | None = None):
    global _db_path, _config, _images_dir, _config_path, _daemon_controller
    global _workshop_folders
    _db_path = db_path
    _config = config
    _config_path = config_path
    _images_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "images")
    # One locator for this server process. It resolves the Steam libraries once
    # and only re-resolves on a miss, so the open-folder route costs one
    # directory check and no registry read per click.
    _workshop_folders = workshop_folders.WorkshopFolders(db_path, config)
    _workshop_folders.log_status()
    # A controller passed by the TUI is shared with its daemon manager; used
    # standalone, this module builds its own from the config path.
    _daemon_controller = daemon_controller or DaemonController(config_path, config=config)
    # The server-side subscribe is a Steam community pull, and this process is
    # not the daemon's, so the debug switch has to be read here too or a
    # subscribe could never be captured. `web_download_switch` is the same
    # lookup the daemon uses, so the deprecated key is honoured in both.
    daemon_config = config.get("daemon", {}) or {}
    capture.configure(
        daemon_config.get("outbox_dir") or daemon_config.get("backup_dir"),
        capture.web_download_switch(daemon_config),
    )


def _get_daemon_controller() -> DaemonController:
    global _daemon_controller
    if _daemon_controller is None:
        _daemon_controller = DaemonController(_config_path, config=_config)
    return _daemon_controller


def bbcode_to_html(text):
    """Converts Steam BBCode to HTML for web display."""
    if not text:
        return ""
    html = re.sub(r'\[h1\](.*?)\[/h1\]', r'<h3>\1</h3>', text, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r'\[h2\](.*?)\[/h2\]', r'<h4>\1</h4>', html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r'\[h3\](.*?)\[/h3\]', r'<h5>\1</h5>', html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r'\[b\](.*?)\[/b\]', r'<b>\1</b>', html, flags=re.IGNORECASE)
    html = re.sub(r'\[i\](.*?)\[/i\]', r'<i>\1</i>', html, flags=re.IGNORECASE)
    html = re.sub(r'\[u\](.*?)\[/u\]', r'<u>\1</u>', html, flags=re.IGNORECASE)
    html = re.sub(r'\[list\]', '<ul>', html, flags=re.IGNORECASE)
    html = re.sub(r'\[/list\]', '</ul>', html, flags=re.IGNORECASE)
    html = re.sub(r'\[\*\](.*?)\n?', r'<li>\1</li>', html, flags=re.IGNORECASE)
    html = re.sub(r'\[table\]', '<table>', html, flags=re.IGNORECASE)
    html = re.sub(r'\[/table\]', '</table>', html, flags=re.IGNORECASE)
    html = re.sub(r'\[tr\]', '<tr>', html, flags=re.IGNORECASE)
    html = re.sub(r'\[/tr\]', '</tr>', html, flags=re.IGNORECASE)
    html = re.sub(r'\[th\](.*?)\[/th\]', r'<th>\1</th>', html, flags=re.IGNORECASE)
    html = re.sub(r'\[td\](.*?)\[/td\]', r'<td>\1</td>', html, flags=re.IGNORECASE)
    html = re.sub(r'\[quote\](.*?)\[/quote\]', r'<blockquote>\1</blockquote>', html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r'\[quote=([^\]]*)\](.*?)\[/quote\]', r'<blockquote><b>\1:</b><br>\2</blockquote>', html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r'\[code\](.*?)\[/code\]', r'<pre><code>\1</code></pre>', html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r'\[img\](.*?)\[/img\]', r'<img src="\1" alt="image">', html, flags=re.IGNORECASE)
    html = re.sub(r'\[url\](.*?)\[/url\]', r'<a href="\1" target="_blank">\1</a>', html, flags=re.IGNORECASE)
    html = re.sub(r'\[url=([^\]]*)\](.*?)\[/url\]', r'<a href="\1" target="_blank">\2</a>', html, flags=re.IGNORECASE)
    html = re.sub(r'\n', '<br>', html)
    return html


@app.route('/images/<path:filename>')
def serve_image(filename):
    # If the filename contains path separators, it is already nested
    if '/' in filename or '\\' in filename:
        return send_from_directory(_images_dir, filename)

    # Otherwise, it's a flat filename like "1039919954.jpg"
    if '.' in filename:
        name_part, ext = filename.rsplit('.', 1)
        try:
            wid = int(name_part)
            from src.database import get_image_subdirs
            bucket1, bucket2, bucket3 = get_image_subdirs(wid)
            nested_path = f"{bucket1}/{bucket2}/{bucket3}/{filename}"
            return send_from_directory(_images_dir, nested_path)
        # Filename stem is not a workshop_id: fall through to the flat image
        # directory below.
        except ValueError:
            # Not an integer workshop_id, serve flat
            pass

    return send_from_directory(_images_dir, filename)


@app.route('/')
def index():
    # The worker owns the default; share its constant so an unset delay does not
    # display a number the decay rule would immediately raise to the floor.
    web_delay = float((_config.get("daemon", {}) or {}).get("web_delay_seconds", WEB_DELAY_DEFAULT))
    import json as _json
    # The open-folder control is Windows-only, so the page is told whether to
    # render it at all: off Windows the button and the `o` shortcut are absent,
    # not merely inert.
    return render_template('index.html', web_delay=web_delay,
                           filter_schema_json=_json.dumps(SEARCH_FILTER_SCHEMA),
                           open_folder_enabled=bool(
                               _workshop_folders and _workshop_folders.is_supported()))


@app.route('/userscript/<path:filename>')
def serve_userscript(filename):
    script_path = os.path.join(os.path.dirname(__file__), '..', 'userscripts', filename)
    if not os.path.isfile(script_path):
        return jsonify({"error": "not found"}), 404

    with open(script_path, 'r', encoding='utf-8') as handle:
        content = handle.read()

    host = request.host
    if host and not host.startswith('127.') and not host.startswith('localhost'):
        base = f"http://{host}"
        metadata_lines = [
            f'// @include      {base}/*',
            f'// @updateURL    {base}/userscript/{filename}',
            f'// @downloadURL  {base}/userscript/{filename}',
        ]
        marker = '// ==/UserScript=='
        content = content.replace(marker, '\n'.join(metadata_lines) + '\n' + marker)

    return content, 200, {'Content-Type': 'application/javascript; charset=utf-8'}


@app.route('/api/search', methods=['POST', 'GET'])
def api_search():
    data = request.get_json(silent=True) or {}
    filters = data.get('filters', [])
    sort_by = data.get('sort_by', 'title')
    sort_order = data.get('sort_order', 'ASC')
    offset = data.get('offset', 0)
    limit = data.get('limit', 50)
    # The Subscribed overlay is a view control: one predicate ANDed onto the
    # builder's rows, never part of the saved scraper filter.
    subscribed_overlay = data.get('subscribed')

    try:
        results = search_items(
            _db_path,
            filters=filters,
            sort_by=sort_by,
            sort_order=sort_order,
            summary_only=True,
            limit=limit,
            offset=offset,
            subscribed_overlay=subscribed_overlay,
        )

        if results:
            image_flagged_count = 0
            for item in results:
                wid = item['workshop_id']
                raise_web_scrape_priority_for_list(_db_path, wid)
                raise_image_priority_for_list(_db_path, wid)
                if _ensure_image_flagged(wid, 5):
                    image_flagged_count += 1
                raise_translation_priority_for_list(_db_path, wid)

            ids = [row['workshop_id'] for row in results]
            conn = get_connection(_db_path)
            placeholders = ','.join('?' * len(ids))
            flag_rows = conn.execute(
                f"SELECT workshop_id, web_scrape_priority, image_priority, translation_priority FROM workshop_items WHERE workshop_id IN ({placeholders})",
                ids
            ).fetchall()
            conn.close()
            flag_map = {row['workshop_id']: dict(row) for row in flag_rows}
            for row in results:
                if row['workshop_id'] in flag_map:
                    flag_row = flag_map[row['workshop_id']]
                    row['web_scrape_priority'] = flag_row['web_scrape_priority']
                    row['image_priority'] = flag_row['image_priority']
                    row['translation_priority'] = flag_row['translation_priority']

            sample = results[0] if results else {}
            logging.info(f"[Search] returned {len(results)} items, flagged {image_flagged_count} for image, sample image_priority={sample.get('image_priority')} image_answer={sample.get('image_answer')!r}")

        return jsonify([_attach_subscription(row) for row in _attach_image_state(results)])
    except Exception as e:
        logging.exception(f"[Search] Error processing search request")
        return jsonify({"error": str(e)}), 500


def _detail_payload(workshop_id):
    """Build the detail response for an item, or None when it does not exist."""
    item = get_item_details(_db_path, workshop_id)
    if not item:
        return None

    # Both language variants travel together so the client can switch between
    # them without another request. The TUI's toggle is a local re-render, and
    # shipping the pair keeps the web equivalent off the network as well.
    item["description_html"] = bbcode_to_html(
        item.get("extended_description_en") or item.get("extended_description") or "")
    item["description_html_original"] = bbcode_to_html(item.get("extended_description") or "")
    item["display_title"] = item.get("title_en") or item.get("title") or "N/A"
    item["display_title_original"] = item.get("title") or item.get("title_en") or "N/A"
    # The TUI offers the toggle only once a translation has been stored, so the
    # client is told outright rather than inferring it from a possibly-empty
    # string: an item can have a translated field that is identical to the
    # original, and `translate_version` is what actually marks it as translated.
    item["has_translation"] = bool(item.get("translate_version"))
    # The creator ID is a SteamID64 — seventeen digits, beyond the range a
    # JavaScript number represents exactly. It travels as a string so the
    # client's jump-to-author filter can name the same account it displays;
    # a numeric field would round silently in JSON.parse.
    if item.get("creator_steamid") is not None:
        item["creator_id"] = str(item["creator_steamid"])
    # The four-state marker is derived here from `src/subscription.py`, once, so
    # the grid and the detail pane cannot disagree about the same item and the
    # page's rendering has a single source for the glyph, colour and tooltip.
    _attach_subscription(item)
    return item


def _attach_subscription(item: dict) -> dict:
    """Attach the shared subscription marker fields to a payload in place.

    `own_subscribed` / `own_first_subscribed_at` stay on the payload as well, so
    the raw columns remain inspectable; `subscription_state` and the glyph,
    colour, class, label and tooltip that accompany it are what the page renders
    from. They are computed in Python rather than in the page so the page owns no
    copy of the glyph/colour table: a marker that disagreed with the TUI about
    what "previously" looks like is the drift the shared module exists to stop.
    """
    state = subscription.subscription_state(item)
    glyph, colour, css, label = subscription.marker_spec(state)
    item["subscription_state"] = state
    item["subscription_glyph"] = glyph
    item["subscription_colour"] = colour
    item["subscription_class"] = css
    item["subscription_label"] = label
    item["subscription_tooltip"] = subscription.tooltip(state)
    item["subscription_clickable"] = subscription.is_clickable(state)
    return item


@app.route('/api/item/<int:workshop_id>')
def api_item(workshop_id):
    """Read-only detail fetch.

    Deliberately free of side effects. The web UI polls this every three seconds
    while a pane is open, and applying detail priority here re-armed the fetch
    queue on every poll: the daemon re-fetched whatever was on screen, forever.
    Opening a pane goes through api_item_open instead, which is the only route
    that applies detail priority.
    """
    item = _detail_payload(workshop_id)
    if item is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(item)


@app.route('/api/item/<int:workshop_id>/open', methods=['POST'])
def api_item_open(workshop_id):
    """Apply detail-level priority once, then return the detail record.

    Separate from the read-only route so the frequent caller cannot re-queue an
    item by accident, and so the behaviour is directly testable.
    """
    raise_web_scrape_priority_for_detail(_db_path, workshop_id)
    raise_image_priority_for_detail(_db_path, workshop_id)
    _ensure_image_flagged(workshop_id, 10)
    raise_translation_priority_for_detail(_db_path, workshop_id)
    raise_api_priority_for_detail(_db_path, workshop_id)

    item = _detail_payload(workshop_id)
    if item is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(item)


@app.route('/api/items', methods=['POST'])
def api_items():
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    if not ids or not isinstance(ids, list):
        return jsonify({"error": "ids list required"}), 400

    conn = get_connection(_db_path)
    placeholders = ','.join('?' * len(ids))
    sql = f"""
        SELECT w.workshop_id, w.title, w.title_en, w.creator_steamid, w.consumer_appid,
               w.translate_version, w.is_queued_for_subscription, w.web_scrape_priority,
               w.image_priority, w.translation_priority, w.file_size, w.image_answer,
               w.wilson_subscription_score, w.wilson_favorite_score,
               w.own_subscribed, w.own_first_subscribed_at, w.steam_download_seen_at,
               w.api_priority,
               u.personaname, u.personaname_en
        FROM workshop_items w LEFT JOIN creators u ON w.creator_steamid = u.steamid
        WHERE w.workshop_id IN ({placeholders})
    """
    results = [_attach_subscription(row) for row in
               _attach_image_state([dict(row) for row in conn.execute(sql, ids).fetchall()])]
    conn.close()
    return jsonify(results)


@app.route('/api/state')
def api_tui_state():
    state_path = os.path.join(os.path.dirname(_db_path), ".tui_state.yaml")
    try:
        import yaml
        with open(state_path, 'r', encoding='utf-8') as handle:
            state = yaml.safe_load(handle) or {}
    except Exception:
        logging.info("No saved filter state found or failed to read")
        state = {}
    return jsonify(state)


@app.route('/api/session')
def api_session():
    """Whether the saved Steam login is still working, for the header warning.

    Read-only and cheap: one small YAML file beside the database. The UI polls
    it, so it must stay a file read -- no request to Steam belongs here.
    """
    problem = session_health.read(_db_path) or {}
    return jsonify({
        "problem": bool(problem),
        "detail": problem.get("detail"),
        "detected_at": problem.get("detected_at"),
        "login_url": session_health.LOGIN_URL,
    })


@app.route('/api/session/recheck', methods=['POST'])
def api_session_recheck():
    """Re-read the login cookie from the browser after the operator signs in.

    The browser is the source the daemon itself prefers, so this is the same
    lookup rather than a second opinion. A cookie that is not locally expired is
    saved and clears the warning; the daemon re-reads config.yaml per request and
    per batch, so it picks the value up without a restart.

    Clearing on an unexpired cookie rather than on a successful request is
    deliberate: proving it would mean spending a request now, when the next
    scrape or reconcile is about to make one anyway and will correct the warning
    if Steam still refuses. Telling the operator the truth sooner is worth more
    than a guarantee this route cannot cheaply give.
    """
    fresh = steam_login_secure(refresh=True) or login_secure_value(_config)

    reason = session_health.evaluate_login(fresh)
    if reason:
        session_health.record_rejected(_db_path, reason)
        return jsonify({"ok": False, "problem": True, "detail": reason})

    if fresh != login_secure_value(_config):
        _config.setdefault("session", {})["login_secure"] = fresh
        try:
            save_config(_config_path, _config)
        except Exception as exc:
            detail = f"a fresh login cookie was found but could not be saved ({exc})"
            session_health.record_rejected(_db_path, detail)
            logging.warning("[Session] %s", detail)
            return jsonify({"ok": False, "problem": True, "detail": detail}), 500

    session_health.record_accepted(_db_path)
    logging.info("[Session] login cookie re-read from the browser; the warning is cleared")
    return jsonify({"ok": True, "problem": False})


@app.route('/api/delete_never_fetched_items', methods=['POST'])
def api_delete_never_fetched_items():
    """Delete every never-successfully-fetched item.

    Deliberately the same predicate and the same delete as the TUI's
    ``action_delete_never_fetched_items``, both through ``delete_never_fetched_items``: the web
    route must not grow its own idea of what "pending" means, and there is no
    dry-run because the TUI has none. The count is returned so the UI can say
    what was removed rather than claiming a generic success.
    """
    deleted = delete_never_fetched_items(_db_path)
    logging.info("[Delete Never Fetched] removed %d never-fetched item(s)", deleted)
    return jsonify({"ok": True, "deleted": deleted})


@app.route('/api/cutoffs', methods=['POST'])
def api_cutoffs():
    data = request.get_json(silent=True) or {}
    filters = data.get('filters', [])
    # The overlay constrains the same population the grid shows, so the
    # percentiles are computed over it as well.
    cutoffs = compute_wilson_cutoffs(_db_path, filters if filters else None,
                                     subscribed_overlay=data.get('subscribed'))
    result = {}
    for k, percentile in cutoffs.items():
        result[k] = percentile
    return jsonify(result)


@app.route('/api/authors')
def api_authors():
    authors = get_all_creator_ids(_db_path)
    return jsonify(authors)


@app.route('/api/tags')
def api_tags():
    # Only the tag metric. This used to compute the entire statistics payload and
    # return one field of it: about five seconds of work for under 2 KB.
    return jsonify(
        metrics.values(metrics.compute(_db_path, ["tag_counts"]))["tag_counts"]
    )


@app.route('/api/stats')
def api_stats():
    """Every statistic at once.

    Kept for diagnosing the database directly. The UI fetches the metrics one at
    a time instead, so each chunk appears as soon as it is ready.
    """
    stats = get_db_stats(_db_path)
    return jsonify(stats)


@app.route('/api/metrics')
def api_metrics_catalogue():
    """What statistics exist, with a seed ordering hint.

    The client draws its layout from this rather than hard-coding a list, so a
    metric added on the server appears without a matching front-end change.

    `seed_ms` is only a first-run ordering hint. The client is expected to
    replace it with the duration it measured last time it asked, so the order
    follows the data instead of this table.
    """
    return jsonify({
        "metrics": metrics.catalogue(),
        "default_order": metrics.all_names(),
    })


@app.route('/api/metrics/<name>')
def api_metric(name):
    """One metric, computed on its own.

    Separate requests are what make the chunks independent: whichever finishes
    first renders first, and a slow metric cannot hold up a fast one.
    """
    if name not in metrics.REGISTRY:
        return jsonify({"error": f"unknown metric {name!r}"}), 404
    # The configured target AppIDs travel with the request so the coverage
    # metric can restrict its second figure to what the owner cares about; a
    # config with none lets the metric fall back to every `app_discovery` row.
    params = {"target_appids": (_config.get("daemon", {}) or {}).get("target_appids")}
    entry = metrics.compute(_db_path, [name], params)[name]
    return jsonify({
        "name": name,
        "value": entry["value"],
        "ms": entry["ms"],
        "note": entry["note"],
        "seed_ms": entry["seed_ms"],
    })


@app.route('/api/analysis')
def api_analysis():
    # The TUI clamps its bucket box to at least one day; the endpoint must too,
    # because a zero width divides by zero and a negative one indexes a negative
    # bucket, so either would answer 500 to a hand-made request.
    bucket = max(1, request.args.get('bucket_days', 7, type=int))
    result = view_window_analysis(_db_path, bucket_days=bucket)
    return jsonify(result)


@app.route('/api/save_filter', methods=['POST'])
def api_save_filter():
    data = request.get_json(silent=True) or {}
    filters = data.get('filters', [])
    appids = _config.get("daemon", {}).get("target_appids", [])
    appid = appids[0] if appids else None
    if appid is None:
        return jsonify({"error": "No target AppID configured"}), 400
    save_enrichment_filters(_db_path, appid, enrichment_filters=json.dumps(filters))
    return jsonify({"ok": True, "appid": appid})


def _ensure_image_flagged(workshop_id, priority):
    """Flag the image for download unless it already has a final answer.

    "Final" means a file exists, or the server has already said the picture is
    not there (a 404, a non-image content type). Re-flagging the latter is how
    opening a detail pane used to put a missing preview back in the queue for
    an item that can never satisfy it.
    """
    conn = get_connection(_db_path)
    row = conn.execute(
        "SELECT preview_url, image_answer, image_priority FROM workshop_items WHERE workshop_id=?",
        (workshop_id,)
    ).fetchone()
    conn.close()
    if row and row["preview_url"] and not images.is_resolved(row["image_answer"]):
        raise_image_priority(_db_path, workshop_id, max(row["image_priority"] or 1, priority))
        return True
    return False


# The sentences and the request shape live in `src/subscribe_engine.py`, which the
# TUI's queue drives too, so the two front ends cannot drift apart.
_SUBSCRIBE_NO_SESSION_MESSAGE = subscribe_engine.NO_SESSION_MESSAGE
_SUBSCRIBE_NO_LOGIN_MESSAGE = subscribe_engine.NO_LOGIN_MESSAGE
_SUBSCRIBE_SESSION_REJECTED_DETAIL = subscribe_engine.SUBSCRIBE_SESSION_REJECTED_DETAIL


@app.route('/api/subscribe/<int:workshop_id>', methods=['POST'])
def api_subscribe(workshop_id):
    """Subscribe to an item against Steam directly, with no browser tab.

    This is the userscript bridge's half of the flow: it sends the request and
    records Steam's answer, while the browser plugin watches the page. The
    browser-free engine in ``src/subscribe_engine.py`` owns the request shape
    and is what the TUI (and the web UI next) drives; this route builds its POST
    from the same helpers rather than keeping a second copy.

    The CSRF token now comes from the item page this route reads, exactly as the
    engine's does. ``sessionid`` is a session cookie Firefox keeps in memory and
    never writes to ``cookies.sqlite``, so the profile read can never carry the
    current one; the page's own ``g_sessionID`` is the token that belongs to the
    credential that authenticated that read. The pushed ``_pushed_sessionid`` global and
    ``session.csrf_token`` are only a fallback for a page that carries no token, which is
    what keeps the userscript-driven flow working for an anonymous page. The read
    is gated on the shared web interval -- ``daemon.web_delay_seconds`` through
    ``configured_web_delay`` and ``pacing.wait`` -- so it honours the same rate
    the daemon's worker and the engine keep. **Nothing else this route decides
    changed**: the same refusal branches, the same status codes, the same
    response bodies. (What a refusal *records* did change; see the block below.)
    """
    cookies, fallback_token, login = subscribe_engine.resolve_subscribe_credentials(
        _config, _pushed_sessionid)
    logging.info(
        f"[Subscribe] request for workshop_id={workshop_id}, "
        f"token_fallback={'set' if fallback_token else 'missing'}, "
        f"login={'set' if login else 'missing'}")
    # Refuse before spending a request: without a login cookie the page read
    # would be anonymous and Steam answers anonymously, which can never
    # subscribe. The message names the remedy, because the person reading it is
    # holding a browser.
    if not login:
        logging.warning(f"[Subscribe] No steamLoginSecure available — refusing before the request")
        return jsonify({"success": -1, "message": _SUBSCRIBE_NO_LOGIN_MESSAGE}), 400

    problem = session_health.evaluate_login(login)
    if problem:
        logging.warning(f"[Subscribe] Refusing expired login for workshop_id={workshop_id}: {problem}")
        session_health.record_rejected(_db_path, problem)
        return jsonify({"success": -1, "message": problem}), 400

    found, appid = subscribe_engine.lookup_consumer_appid(_db_path, workshop_id)
    if not found:
        logging.warning(f"[Subscribe] No item row for workshop_id={workshop_id} — cannot subscribe")
        return jsonify({"success": -1, "message": "Item not found."}), 404

    if not appid:
        logging.warning(f"[Subscribe] Item {workshop_id} has no AppID — cannot subscribe")
        return jsonify({"success": -1, "message": "Item has no AppID."}), 400

    # The token source: read the item page this request is about to act on, on
    # the shared interval. The same attempt's read also decides what a Steam
    # refusal means below -- an authenticated page proves the login is good.
    interval = subscribe_engine.WebInterval(_config, config_path=_config_path)
    page = subscribe_engine.fetch_item_page(workshop_id, interval=interval)
    page_html = subscribe_engine.page_body(page)
    page_authenticated = subscribe_engine.page_read_authenticated(page_html)
    sid, page_token = subscribe_engine.resolve_subscribe_token(
        page_html, cookies, fallback_token)
    if not sid:
        logging.warning(f"[Subscribe] No sessionid available — refusing before the request")
        return jsonify({"success": -1, "message": _SUBSCRIBE_NO_SESSION_MESSAGE}), 400

    logging.info(
        f"[Subscribe] POSTing to Steam: id={workshop_id}, appid={appid}, "
        f"{subscribe_engine.token_log_note(sid, page_token, fallback_token)}, "
        f"login={'set' if login else 'missing'}")
    try:
        # The shared request shape and the shared session, so the TCP connection
        # and TLS handshake are reused the way a browser reuses them; a fresh
        # handshake per call is itself a non-browser signal (`src/web_scraper.py`).
        resp = subscribe_engine.post_subscribe_request(cookies, sid, appid, workshop_id)
        data = resp.json()
        logging.info(f"[Subscribe] Steam response: status={resp.status_code}, body={data}")
        success = data.get("success")
        # Steam's refusal answers. Whether that is a session problem depends on
        # the page this attempt itself read: an authenticated page proves the
        # credential works, so the refusal is about the CSRF token and recording
        # a session problem would send the operator to sign in again for nothing.
        if getattr(resp, "status_code", None) == 401 or success in (2, 15):
            if page_authenticated:
                logging.warning(
                    f"[Subscribe] Steam refused the CSRF token for "
                    f"workshop_id={workshop_id}, but this attempt's page read was "
                    f"authenticated; the login is good, so no session problem "
                    f"was recorded.")
            else:
                session_health.record_rejected(
                    _db_path, _SUBSCRIBE_SESSION_REJECTED_DETAIL)
        elif success == 1:
            # The confirmation is the same fact `/api/subscribed/<id>` stamps for
            # the browser bridge: mark it subscribed and clear the queue flag.
            # The engine's verified branch records it through
            # `subscribe_engine.record_confirmed_subscription`.
            mark_own_subscribed(_db_path, workshop_id)
            session_health.record_accepted(_db_path)
        return jsonify(data)
    except Exception as e:
        logging.warning(f"[Subscribe] failed for workshop_id={workshop_id}: {e}")
        return jsonify({"success": -1, "message": f"Subscribe request failed: {e}"}), 502


@app.route('/api/sessionid', methods=['POST'])
def api_sessionid():
    global _pushed_sessionid
    data = request.get_json(silent=True) or {}
    sid = data.get("sessionid", "").strip()
    login_secure = data.get("login_secure", "").strip()
    if not sid:
        return jsonify({"ok": False, "message": "No sessionid provided."}), 400

    _pushed_sessionid = sid
    # A pushed token and a refreshed login cookie may match nothing in the
    # config or the browser profile, so hand both to the crash reporter now: a
    # crash inside the subscribe route would otherwise write them verbatim.
    crash.register_secret(sid)
    if login_secure:
        crash.register_secret(login_secure)
    # Only the login cookie is persisted; the CSRF token stays in memory. The
    # bridge re-pushes on a timer, and a cookie is valid for days, so a push that
    # carries the value already on disk must not rewrite the config file. That
    # rewrite is the expensive half of the old handler: YAML serialisation and a
    # file write every thirty seconds, per open Steam tab, for no change.
    changed = bool(login_secure) and login_secure_value(_config) != login_secure
    if not login_secure:
        persist_state = "missing"
    elif not changed:
        persist_state = "unchanged"
    else:
        _config.setdefault("session", {})["login_secure"] = login_secure
        # Persist it so the daemon sees it. The daemon is a separate process
        # from this web server, and the web scraper re-reads config.yaml for
        # each request, so this is what makes a refreshed login cookie take
        # effect without restarting anything.
        try:
            save_config(_config_path, _config)
            persist_state = "set and persisted"
        except Exception as exc:
            persist_state = f"set but not persisted ({exc})"
        # A push carries the operator's own live credential, so it is the best
        # evidence available here that the login works again. Judged from the
        # token alone -- Steam is not asked -- which is the same rule the
        # recheck route uses, and a value that is not expired clears the warning.
        if session_health.evaluate_login(login_secure) is None:
            session_health.record_accepted(_db_path)

    # A push that changed nothing is worth a debug line, not an info one.
    log_line = logging.info if changed else logging.debug
    log_line("SessionID updated from userscript (login_secure: %s)", persist_state)
    return jsonify({"ok": True})


@app.route('/api/toggle_subscription_queue/<int:workshop_id>', methods=['POST'])
def api_toggle_subscription_queue(workshop_id):
    toggle_subscription_queue(_db_path, workshop_id)
    return jsonify({"ok": True})


@app.route('/api/subscribed/<int:workshop_id>', methods=['POST'])
def api_subscribed(workshop_id):
    """The userscript confirming a subscribe that Steam accepted.

    This is the highest-fidelity signal the project gets: it fires the instant
    the subscribe lands, so the marker is right before the next reconcile, and
    the stamp is made from a confirmed subscribe rather than a page scrape.
    ``mark_own_subscribed`` also clears the queue flag -- there is nothing
    pending for an item that is now subscribed -- which is what the route used
    to do alone, throwing the subscription fact away.
    """
    mark_own_subscribed(_db_path, workshop_id)
    return jsonify({"ok": True})


_subscribe_failures = set()  # in-memory set of workshop_ids that failed subscription


@app.route('/api/subscribe_failed/<int:workshop_id>', methods=['POST'])
def api_subscribe_failed(workshop_id):
    clear_subscription_queue(_db_path, workshop_id)
    _subscribe_failures.add(workshop_id)
    return jsonify({"ok": True})


# Shipping a throttle as a failure was wrong twice over: the item was never
# attempted, and clearing it from the queue threw away the work the drain had
# queued up. A throttled item therefore stays queued, and the UI stops opening
# tabs until the request budget refills — Steam's is per account or address and
# refills over minutes.
SUBSCRIBE_THROTTLE_PAUSE_SECONDS = 300.0
_subscribe_throttled_at = 0.0
_subscribe_throttled_id = None


@app.route('/api/subscribe_throttled/<int:workshop_id>', methods=['POST'])
def api_subscribe_throttled(workshop_id):
    global _subscribe_throttled_at, _subscribe_throttled_id
    _subscribe_throttled_at = time.time()
    _subscribe_throttled_id = workshop_id
    logging.warning(
        "[Subscribe] Steam throttled the request for workshop_id=%s; it stays queued "
        "and is retried once the budget refills.", workshop_id)
    return jsonify({"ok": True, "retry_after": SUBSCRIBE_THROTTLE_PAUSE_SECONDS})


@app.route('/api/subscribe_throttle')
def api_subscribe_throttle():
    """Whether Steam is currently refusing us, and for how much longer."""
    return jsonify({
        "throttled_at": _subscribe_throttled_at,
        "throttled_id": _subscribe_throttled_id,
        "retry_after": SUBSCRIBE_THROTTLE_PAUSE_SECONDS,
    })


@app.route('/api/subscribe_failures')
def api_subscribe_failures():
    return jsonify(sorted(_subscribe_failures))


@app.route('/api/fetch_new', methods=['POST'])
def api_fetch_new():
    with open('.fetch_new', 'w') as handle:
        handle.write('1')
    return jsonify({"ok": True})


@app.route('/api/update_visible', methods=['POST'])
def api_update_visible():
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    if not ids or not isinstance(ids, list):
        return jsonify({"error": "ids list required"}), 400
    conn = get_connection(_db_path)
    placeholders = ','.join('?' * len(ids))
    conn.execute(
        f"UPDATE workshop_items SET api_priority = 10 WHERE workshop_id IN ({placeholders}) AND (fetch_status IS NULL OR fetch_status != -1)",
        ids,
    )
    updated = conn.total_changes
    conn.commit()
    conn.close()
    return jsonify({"queued": updated})


@app.route('/api/queued')
def api_queued():
    # The queue overlay draws each row's real marker from this payload, the same
    # table the grid and the TUI's queue screen read; without the derived fields
    # the overlay could not draw `downloaded` or any other state.
    items = [_attach_subscription(item) for item in get_subscription_queue_items(_db_path)]
    return jsonify(items)


@app.route('/api/open_folder/<int:workshop_id>', methods=['POST'])
def api_open_folder(workshop_id):
    """Open a downloaded item's workshop folder in Explorer.

    Explorer is opened **on the host running the server**, not in the browser:
    the click travels here as a POST and the folder opens on this machine's
    desktop. The shared helper owns every guard -- Windows only, the item must
    be in the ``downloaded`` state, and the folder must still be on disk -- and
    a refusal names the reason so the page can show it. Nothing is launched when
    the folder is missing, and no route here changes the item's state.
    """
    helper = _workshop_folders
    if helper is None:
        return jsonify({"ok": False, "folder": None,
                        "message": "The folder helper is not initialised on this server."}), 400
    result = helper.open_folder(workshop_id)
    status = 200 if result["ok"] else 400
    return jsonify(result), status


@app.route('/api/pause', methods=['POST'])
def api_pause():
    # Creating the lock is also how the pause interval is recorded for the
    # drain estimate's active-time rate; see src/activity.py.
    activity.begin_pause('.pauselock', _db_path, source="web_subscribe")
    return jsonify({"ok": True})


@app.route('/api/resume', methods=['POST'])
def api_resume():
    # Idempotent resume: an absent pause lock is the desired end state, so
    # `end_pause` is a no-op success when it is already gone.
    activity.end_pause('.pauselock', _db_path)
    return jsonify({"ok": True})


@app.route('/api/daemon')
def api_daemon():
    controller = _get_daemon_controller()
    status = controller.status()
    status["log_file"] = controller.log_file()
    return jsonify(status)


@app.route('/api/daemon/start', methods=['POST'])
def api_daemon_start():
    changed, message = _get_daemon_controller().start()
    return jsonify({"ok": True, "changed": changed, "message": message})


@app.route('/api/daemon/stop', methods=['POST'])
def api_daemon_stop():
    changed, message = _get_daemon_controller().stop()
    return jsonify({"ok": True, "changed": changed, "message": message})


@app.route('/api/daemon/restart', methods=['POST'])
def api_daemon_restart():
    changed, message = _get_daemon_controller().restart()
    return jsonify({"ok": True, "changed": changed, "message": message})


@app.route('/api/daemon/log')
def api_daemon_log():
    # A page cached before the Batch 4 rename still sends `since`; it is the
    # same byte offset under its new name.
    since_offset = request.args.get(
        'since_offset', request.args.get('since', 0, type=int), type=int) or 0
    return jsonify(_get_daemon_controller().tail_log(since_offset))


# Legacy route aliases. ``templates/index.html`` is served from the browser
# cache, so a tab opened before the Batch 4 rename still calls these paths; each
# answers through the renamed view function. Nothing outside this repo's own
# front ends reads them, and they can go once a deploy has propagated.
app.add_url_rule('/api/toggle_sub/<int:workshop_id>',
                 view_func=api_toggle_subscription_queue, methods=['POST'])
app.add_url_rule('/api/sub_health', view_func=api_subscribe_throttle)
app.add_url_rule('/api/sub_failures', view_func=api_subscribe_failures)
app.add_url_rule('/api/clear_pending', view_func=api_delete_never_fetched_items,
                 methods=['POST'])
