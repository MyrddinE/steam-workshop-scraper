"""Embedded web server for Steam Workshop Scraper."""

import json
import os
import time
import re
import logging
from flask import Flask, request, jsonify, render_template, send_from_directory
from src.database import search_items, get_item_details, get_db_stats, get_all_authors, save_app_filter, compute_wilson_cutoffs, bump_web_priority_for_list, bump_web_priority_for_detail, bump_translation_for_list, bump_translation_for_detail, bump_image_priority_for_list, bump_image_priority_for_detail, flag_for_image, get_connection, toggle_subscription_queue_status, clear_subscription_queue_status, mark_own_subscribed, get_queued_items, FILTER_SCHEMA, bump_api_priority_for_detail, clear_pending_items
from src.analysis import view_window_analysis
from src import capture
from src import crash
from src import images
from src import metrics
from src import session_health
from src import subscription
from src import web_scraper
from src.config import login_secure_value, save_config
from src.daemon_control import DaemonController
from src.firefox_cookies import steam_login_secure
from src.web_worker import WEB_DELAY_DEFAULT

app = Flask(__name__, template_folder='../templates')
app.config['TEMPLATES_AUTO_RELOAD'] = True


def _with_image_state(rows):
    """Attach the image classification the grid branches on.

    Computed here from `src/images.py` rather than left to the browser: if the
    page re-derived the rule from the raw column it would need its own copy of
    the extension allowlist, and the writer and the reader disagreeing about
    what counts as a picture is the bug this exists to prevent.
    """
    for row in rows:
        state = images.image_state(row.get("image_extension"))
        row["image_state"] = state
        # Sent alongside so the page never has to know which states count as
        # settled; only src/images.py decides that.
        row["image_resolved"] = state in (images.PRESENT, images.PERMANENT, images.OTHER)
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
_sessionid = ""
_config_path = "config.yaml"
_daemon_controller = None


def init_webserver(db_path: str, config: dict, config_path: str = "config.yaml",
                   daemon_controller: DaemonController | None = None):
    global _db_path, _config, _images_dir, _config_path, _daemon_controller
    _db_path = db_path
    _config = config
    _config_path = config_path
    _images_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "images")
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


def _bbcode_to_html(text):
    """Converts Steam BBCode to HTML for web display."""
    if not text:
        return ""
    t = re.sub(r'\[h1\](.*?)\[/h1\]', r'<h3>\1</h3>', text, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'\[h2\](.*?)\[/h2\]', r'<h4>\1</h4>', t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'\[h3\](.*?)\[/h3\]', r'<h5>\1</h5>', t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'\[b\](.*?)\[/b\]', r'<b>\1</b>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[i\](.*?)\[/i\]', r'<i>\1</i>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[u\](.*?)\[/u\]', r'<u>\1</u>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[list\]', '<ul>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[/list\]', '</ul>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[\*\](.*?)\n?', r'<li>\1</li>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[table\]', '<table>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[/table\]', '</table>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[tr\]', '<tr>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[/tr\]', '</tr>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[th\](.*?)\[/th\]', r'<th>\1</th>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[td\](.*?)\[/td\]', r'<td>\1</td>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[quote\](.*?)\[/quote\]', r'<blockquote>\1</blockquote>', t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'\[quote=([^\]]*)\](.*?)\[/quote\]', r'<blockquote><b>\1:</b><br>\2</blockquote>', t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'\[code\](.*?)\[/code\]', r'<pre><code>\1</code></pre>', t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'\[img\](.*?)\[/img\]', r'<img src="\1" alt="image">', t, flags=re.IGNORECASE)
    t = re.sub(r'\[url\](.*?)\[/url\]', r'<a href="\1" target="_blank">\1</a>', t, flags=re.IGNORECASE)
    t = re.sub(r'\[url=([^\]]*)\](.*?)\[/url\]', r'<a href="\1" target="_blank">\2</a>', t, flags=re.IGNORECASE)
    t = re.sub(r'\n', '<br>', t)
    return t


def _format_count(n):
    if not n or n == 0:
        return "0"
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        if n < 10_000:
            return f"{n/1000:.2f}K"
        elif n < 100_000:
            return f"{n/1000:.1f}K"
        return f"{n/1000:.0f}K"
    v = n / 1_000_000
    if n < 10_000_000:
        return f"{v:.2f}M"
    elif n < 100_000_000:
        return f"{v:.1f}M"
    return f"{v:.0f}M"


def _format_size(size_bytes):
    if not size_bytes:
        return "N/A"
    size = float(size_bytes)
    kb = size / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb/1024:.1f} GB"


@app.template_filter('fcount')
def template_fcount(n):
    return _format_count(n)


@app.template_filter('fsize')
def template_fsize(n):
    return _format_size(n)


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
            char1, char2, char3 = get_image_subdirs(wid)
            nested_path = f"{char1}/{char2}/{char3}/{filename}"
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
    return render_template('index.html', web_delay=web_delay,
                           filter_schema_json=_json.dumps(FILTER_SCHEMA))


@app.route('/userscript/<path:filename>')
def serve_userscript(filename):
    template_path = os.path.join(os.path.dirname(__file__), '..', 'userscripts', filename)
    if not os.path.isfile(template_path):
        return jsonify({"error": "not found"}), 404

    with open(template_path, 'r', encoding='utf-8') as f:
        content = f.read()

    host = request.host
    if host and not host.startswith('127.') and not host.startswith('localhost'):
        base = f"http://{host}"
        extras = [
            f'// @include      {base}/*',
            f'// @updateURL    {base}/userscript/{filename}',
            f'// @downloadURL  {base}/userscript/{filename}',
        ]
        marker = '// ==/UserScript=='
        content = content.replace(marker, '\n'.join(extras) + '\n' + marker)

    return content, 200, {'Content-Type': 'application/javascript; charset=utf-8'}


@app.route('/api/search', methods=['POST', 'GET'])
def api_search():
    data = request.get_json(silent=True) or {}
    filters = data.get('filters', [])
    sort_by = data.get('sort_by', 'title')
    sort_order = data.get('sort_order', 'ASC')
    offset = data.get('offset', 0)
    limit = data.get('limit', 50)

    try:
        results = search_items(
            _db_path,
            filters=filters,
            sort_by=sort_by,
            sort_order=sort_order,
            summary_only=True,
            limit=limit,
            offset=offset,
        )

        if results:
            image_flagged_count = 0
            for item in results:
                wid = item['workshop_id']
                bump_web_priority_for_list(_db_path, wid)
                bump_image_priority_for_list(_db_path, wid)
                if _ensure_image_flagged(wid, 5):
                    image_flagged_count += 1
                bump_translation_for_list(_db_path, wid)

            ids = [r['workshop_id'] for r in results]
            conn = get_connection(_db_path)
            placeholders = ','.join('?' * len(ids))
            updated = conn.execute(
                f"SELECT workshop_id, needs_web_scrape, needs_image, translation_priority FROM workshop_items WHERE workshop_id IN ({placeholders})",
                ids
            ).fetchall()
            conn.close()
            updated_map = {row['workshop_id']: dict(row) for row in updated}
            for r in results:
                if r['workshop_id'] in updated_map:
                    u = updated_map[r['workshop_id']]
                    r['needs_web_scrape'] = u['needs_web_scrape']
                    r['needs_image'] = u['needs_image']
                    r['translation_priority'] = u['translation_priority']

            sample = results[0] if results else {}
            logging.info(f"[Search] returned {len(results)} items, flagged {image_flagged_count} for image, sample needs_image={sample.get('needs_image')} image_extension={sample.get('image_extension')!r}")

        return jsonify([_attach_subscription(r) for r in _with_image_state(results)])
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
    item["description_html"] = _bbcode_to_html(
        item.get("extended_description_en") or item.get("extended_description") or "")
    item["description_html_original"] = _bbcode_to_html(item.get("extended_description") or "")
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
    if item.get("creator") is not None:
        item["creator_id"] = str(item["creator"])
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
    glyph, colour, css, label = subscription.spec(state)
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
    bump_web_priority_for_detail(_db_path, workshop_id)
    bump_image_priority_for_detail(_db_path, workshop_id)
    _ensure_image_flagged(workshop_id, 10)
    bump_translation_for_detail(_db_path, workshop_id)
    bump_api_priority_for_detail(_db_path, workshop_id)

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
        SELECT w.workshop_id, w.title, w.title_en, w.creator, w.consumer_appid,
               w.translate_version, w.is_queued_for_subscription, w.needs_web_scrape,
               w.needs_image, w.translation_priority, w.file_size, w.image_extension,
               w.wilson_subscription_score, w.wilson_favorite_score,
               w.own_subscribed, w.own_first_subscribed_at,
               w.api_priority,
               u.personaname, u.personaname_en
        FROM workshop_items w LEFT JOIN users u ON w.creator = u.steamid
        WHERE w.workshop_id IN ({placeholders})
    """
    results = [_attach_subscription(r) for r in
               _with_image_state([dict(r) for r in conn.execute(sql, ids).fetchall()])]
    conn.close()
    return jsonify(results)


@app.route('/api/state')
def api_state():
    state_path = os.path.join(os.path.dirname(_db_path), ".tui_state.yaml")
    try:
        import yaml
        with open(state_path, 'r', encoding='utf-8') as f:
            state = yaml.safe_load(f) or {}
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


@app.route('/api/clear_pending', methods=['POST'])
def api_clear_pending():
    """Delete every never-successfully-fetched item.

    Deliberately the same predicate and the same delete as the TUI's
    ``action_clear_pending``, both through ``clear_pending_items``: the web
    route must not grow its own idea of what "pending" means, and there is no
    dry-run because the TUI has none. The count is returned so the UI can say
    what was removed rather than claiming a generic success.
    """
    deleted = clear_pending_items(_db_path)
    logging.info("[Clear Pending] removed %d pending item(s)", deleted)
    return jsonify({"ok": True, "deleted": deleted})


@app.route('/api/cutoffs', methods=['POST'])
def api_cutoffs():
    data = request.get_json(silent=True) or {}
    filters = data.get('filters', [])
    cutoffs = compute_wilson_cutoffs(_db_path, filters if filters else None)
    result = {}
    for k, v in cutoffs.items():
        result[k] = v
    return jsonify(result)


@app.route('/api/authors')
def api_authors():
    authors = get_all_authors(_db_path)
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
    entry = metrics.compute(_db_path, [name])[name]
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
    save_app_filter(_db_path, appid, enrichment_filters=json.dumps(filters))
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
        "SELECT preview_url, image_extension, needs_image FROM workshop_items WHERE workshop_id=?",
        (workshop_id,)
    ).fetchone()
    conn.close()
    if row and row["preview_url"] and not images.is_resolved(row["image_extension"]):
        flag_for_image(_db_path, workshop_id, max(row["needs_image"] or 1, priority))
        return True
    return False


# The remedy both pre-flight refusals name: the operator reading it is holding a
# browser, so it points at the two things that put a credential where this
# process can find it, not at the branch that noticed one was missing.
_SUBSCRIBE_REMEDY = (
    "Sign in to Steam in the browser the daemon reads cookies from, or "
    "configure session.login_secure."
)
_SUBSCRIBE_NO_SESSION_MESSAGE = "No Steam session configured. " + _SUBSCRIBE_REMEDY
_SUBSCRIBE_NO_LOGIN_MESSAGE = "No Steam login cookie is available. " + _SUBSCRIBE_REMEDY
# Steam answers 2 and 15 to a subscribe whose session is no longer accepted or
# not permitted; both are recorded as a session problem with the remedy.
_SUBSCRIBE_SESSION_REJECTED_DETAIL = (
    "Steam refused the subscribe request, so the saved login cookie is no "
    "longer accepted. " + _SUBSCRIBE_REMEDY
)


@app.route('/api/subscribe/<int:workshop_id>', methods=['POST'])
def api_subscribe(workshop_id):
    """Subscribe to an item against Steam directly, with no browser tab.

    The cookies and the CSRF token come from one read of the cookie source, so
    the credential and the token cannot belong to different sessions; a Steam
    ``sessionid`` from one login beside the credential of another is rejected in
    a way that looks like an ordinary failure (``web_scraper._session_id``). The
    pushed ``_sessionid`` global and the configured id are only a fallback for a
    cookie set that carries no ``sessionid``, which is what keeps the
    userscript-driven flow working exactly as before.
    """
    cookies = web_scraper._build_workshop_cookies(_config)
    sid = cookies.get("sessionid") or _sessionid or _config.get("session", {}).get("id", "")
    # The request has to carry whatever token the form field uses, so a fallback
    # taken from the global or the config is put back into the cookie set.
    if sid and not cookies.get("sessionid"):
        cookies["sessionid"] = sid
    login = cookies.get("steamLoginSecure", "")
    logging.info(
        f"[Subscribe] request for workshop_id={workshop_id}, "
        f"sessionid={'set' if sid else 'missing'}, login={'set' if login else 'missing'}")
    # Refuse before spending a request: without either half of the pair Steam
    # answers anonymously, which can never subscribe. The message names the
    # remedy, because the person reading it is holding a browser.
    if not sid:
        logging.warning(f"[Subscribe] No sessionid available — refusing before the request")
        return jsonify({"success": -1, "message": _SUBSCRIBE_NO_SESSION_MESSAGE}), 400
    if not login:
        logging.warning(f"[Subscribe] No steamLoginSecure available — refusing before the request")
        return jsonify({"success": -1, "message": _SUBSCRIBE_NO_LOGIN_MESSAGE}), 400

    problem = session_health.evaluate_login(login)
    if problem:
        logging.warning(f"[Subscribe] Refusing expired login for workshop_id={workshop_id}: {problem}")
        session_health.record_rejected(_db_path, problem)
        return jsonify({"success": -1, "message": problem}), 400

    conn = get_connection(_db_path)
    row = conn.execute(
        "SELECT consumer_appid FROM workshop_items WHERE workshop_id=?",
        (workshop_id,)
    ).fetchone()
    conn.close()

    if not row:
        logging.warning(f"[Subscribe] No item row for workshop_id={workshop_id} — cannot subscribe")
        return jsonify({"success": -1, "message": "Item not found."}), 404

    appid = row["consumer_appid"]
    if not appid:
        logging.warning(f"[Subscribe] Item {workshop_id} has no AppID — cannot subscribe")
        return jsonify({"success": -1, "message": "Item has no AppID."}), 400

    logging.info(f"[Subscribe] POSTing to Steam: id={workshop_id}, appid={appid}, sessionid={sid[:6]}..., login={'set' if login else 'missing'}")
    # One identity for the whole process: the UA is the project's own, derived
    # from the installed Firefox, because the cookies in the jar came from that
    # browser. A subscribe claiming to be Chrome beside Firefox cookies is the
    # kind of contradiction this project removed from the scrape path.
    headers = {
        "User-Agent": web_scraper.USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": web_scraper.BROWSER_HEADERS["Accept-Language"],
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://steamcommunity.com",
        "Referer": f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}",
        # The fetch metadata of a same-origin XHR/form POST, not of a
        # navigation: `BROWSER_HEADERS` describes a top-level page load
        # (`navigate`/`document`) and copying it here would be a lie.
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        # Inferred, not measured: Steam's community front-end posts this endpoint
        # through jQuery, which sets this header, but no capture of this request
        # exists in this repository. Named as inferred so it is not read as an
        # observation.
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        # The shared session, so the TCP connection and TLS handshake are reused
        # the way a browser reuses them; a fresh handshake per call is itself a
        # non-browser signal (`src/web_scraper.py`).
        session = web_scraper._get_session()
        subscribe_url = "https://steamcommunity.com/sharedfiles/subscribe"
        form = {
            "id": str(workshop_id),
            "appid": str(appid),
            "include_dependencies": "false",
            "sessionid": sid,
        }
        resp = session.post(
            subscribe_url,
            data=form,
            cookies=cookies,
            headers=headers,
            timeout=15,
        )
        # Recorded before the body is parsed, so a non-JSON answer is captured
        # too. The values are the ones just sent -- the form, the jar and the
        # headers are the same objects the session received -- rather than a
        # reconstruction of them.
        if capture.web_download_capture_active():
            capture.record_web_download(
                capture.SUBSCRIBE_KIND, workshop_id, subscribe_url,
                {
                    "request": {"method": "POST", "url": subscribe_url,
                                "headers": headers, "cookies": cookies, "data": form},
                    "http_status": getattr(resp, "status_code", None),
                    "final_url": getattr(resp, "url", "") or subscribe_url,
                    "response_headers": getattr(resp, "headers", None),
                    "body": getattr(resp, "text", None),
                },
            )
        data = resp.json()
        logging.info(f"[Subscribe] Steam response: status={resp.status_code}, body={data}")
        success = data.get("success")
        # Steam's "the session is gone / not permitted" answers are the same ones
        # the TUI maps to a session warning; recording them here is what makes
        # the web UI's banner say it too, without a scrape having to notice.
        if success in (2, 15):
            session_health.record_rejected(_db_path, _SUBSCRIBE_SESSION_REJECTED_DETAIL)
        elif success == 1:
            # The confirmation is the same fact `/api/subscribed/<id>` stamps for
            # the browser bridge: mark it subscribed and clear the queue flag.
            mark_own_subscribed(_db_path, workshop_id)
            session_health.record_accepted(_db_path)
        return jsonify(data)
    except Exception as e:
        logging.warning(f"[Subscribe] failed for workshop_id={workshop_id}: {e}")
        return jsonify({"success": -1, "message": f"Subscribe request failed: {e}"}), 502


@app.route('/api/sessionid', methods=['POST'])
def api_sessionid():
    global _sessionid
    data = request.get_json(silent=True) or {}
    sid = data.get("sessionid", "").strip()
    login_secure = data.get("login_secure", "").strip()
    if not sid:
        return jsonify({"ok": False, "message": "No sessionid provided."}), 400

    _sessionid = sid
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
        state = "missing"
    elif not changed:
        state = "unchanged"
    else:
        _config.setdefault("session", {})["login_secure"] = login_secure
        # Persist it so the daemon sees it. The daemon is a separate process
        # from this web server, and the web scraper re-reads config.yaml for
        # each request, so this is what makes a refreshed login cookie take
        # effect without restarting anything.
        try:
            save_config(_config_path, _config)
            state = "set and persisted"
        except Exception as exc:
            state = f"set but not persisted ({exc})"
        # A push carries the operator's own live credential, so it is the best
        # evidence available here that the login works again. Judged from the
        # token alone -- Steam is not asked -- which is the same rule the
        # recheck route uses, and a value that is not expired clears the warning.
        if session_health.evaluate_login(login_secure) is None:
            session_health.record_accepted(_db_path)

    # A push that changed nothing is worth a debug line, not an info one.
    log = logging.info if changed else logging.debug
    log("SessionID updated from userscript (login_secure: %s)", state)
    return jsonify({"ok": True})


@app.route('/api/toggle_sub/<int:workshop_id>', methods=['POST'])
def api_toggle_sub(workshop_id):
    toggle_subscription_queue_status(_db_path, workshop_id)
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


_sub_failures = set()  # in-memory set of workshop_ids that failed subscription


@app.route('/api/subscribe_failed/<int:workshop_id>', methods=['POST'])
def api_subscribe_failed(workshop_id):
    clear_subscription_queue_status(_db_path, workshop_id)
    _sub_failures.add(workshop_id)
    return jsonify({"ok": True})


# Shipping a throttle as a failure was wrong twice over: the item was never
# attempted, and clearing it from the queue threw away the work the drain had
# queued up. A throttled item therefore stays queued, and the UI stops opening
# tabs until the request budget refills — Steam's is per account or address and
# refills over minutes.
SUBSCRIBE_THROTTLE_PAUSE_SECONDS = 300.0
_sub_throttled_at = 0.0
_sub_throttled_id = None


@app.route('/api/subscribe_throttled/<int:workshop_id>', methods=['POST'])
def api_subscribe_throttled(workshop_id):
    global _sub_throttled_at, _sub_throttled_id
    _sub_throttled_at = time.time()
    _sub_throttled_id = workshop_id
    logging.warning(
        "[Subscribe] Steam throttled the request for workshop_id=%s; it stays queued "
        "and is retried once the budget refills.", workshop_id)
    return jsonify({"ok": True, "retry_after": SUBSCRIBE_THROTTLE_PAUSE_SECONDS})


@app.route('/api/sub_health')
def api_sub_health():
    """Whether Steam is currently refusing us, and for how much longer."""
    return jsonify({
        "throttled_at": _sub_throttled_at,
        "throttled_id": _sub_throttled_id,
        "retry_after": SUBSCRIBE_THROTTLE_PAUSE_SECONDS,
    })


@app.route('/api/sub_failures')
def api_sub_failures():
    return jsonify(sorted(_sub_failures))


@app.route('/api/fetch_new', methods=['POST'])
def api_fetch_new():
    with open('.fetch_new', 'w') as f:
        f.write('1')
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
        f"UPDATE workshop_items SET api_priority = 10 WHERE workshop_id IN ({placeholders}) AND (status IS NULL OR status != -1)",
        ids,
    )
    updated = conn.total_changes
    conn.commit()
    conn.close()
    return jsonify({"queued": updated})


@app.route('/api/queued')
def api_queued():
    items = get_queued_items(_db_path)
    return jsonify(items)


@app.route('/api/pause', methods=['POST'])
def api_pause():
    with open('.pauselock', 'w') as f:
        f.write('1')
    return jsonify({"ok": True})


@app.route('/api/resume', methods=['POST'])
def api_resume():
    try:
        os.remove('.pauselock')
    # Idempotent resume: an absent pause lock is the desired end state, so the
    # remove is a no-op success.
    except FileNotFoundError:
        pass
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
    since = request.args.get('since', 0, type=int) or 0
    return jsonify(_get_daemon_controller().tail_log(since))
