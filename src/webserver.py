"""Embedded web server for Steam Workshop Scraper."""

import json
import os
import time
import re
import logging
import requests
from flask import Flask, request, jsonify, render_template, send_from_directory
from src.database import search_items, get_item_details, get_db_stats, get_all_authors, save_app_filter, compute_wilson_cutoffs, bump_web_priority_for_list, bump_web_priority_for_detail, bump_translation_for_list, bump_translation_for_detail, bump_image_priority_for_list, bump_image_priority_for_detail, flag_for_image, get_connection, toggle_subscription_queue_status, clear_subscription_queue_status, get_queued_items, FILTER_SCHEMA, bump_api_priority_for_detail
from src.analysis import view_window_analysis
from src import metrics
from src.config import login_secure_value, save_config
from src.daemon_control import DaemonController

app = Flask(__name__, template_folder='../templates')
app.config['TEMPLATES_AUTO_RELOAD'] = True
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
    web_delay = float((_config.get("daemon", {}) or {}).get("web_delay_seconds", 5.0))
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

        return jsonify(results)
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
               w.api_priority,
               u.personaname, u.personaname_en
        FROM workshop_items w LEFT JOIN users u ON w.creator = u.steamid
        WHERE w.workshop_id IN ({placeholders})
    """
    results = [dict(r) for r in conn.execute(sql, ids).fetchall()]
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
    bucket = request.args.get('bucket_days', 7, type=int)
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
    """If item has preview_url but no image_extension, flag it for download."""
    conn = get_connection(_db_path)
    row = conn.execute(
        "SELECT preview_url, image_extension, needs_image FROM workshop_items WHERE workshop_id=?",
        (workshop_id,)
    ).fetchone()
    conn.close()
    if row and row["preview_url"] and not row["image_extension"]:
        flag_for_image(_db_path, workshop_id, max(row["needs_image"] or 1, priority))
        return True
    return False


@app.route('/api/subscribe/<int:workshop_id>', methods=['POST'])
def api_subscribe(workshop_id):
    global _sessionid
    sid = _sessionid or _config.get("session", {}).get("id", "")
    logging.info(f"[Subscribe] request for workshop_id={workshop_id}, sessionid={'set' if sid else 'missing'}")
    if not sid:
        logging.warning(f"[Subscribe] No sessionid available — userscript may not have pushed one")
        return jsonify({"success": -1, "message": "No Steam session configured."}), 400

    conn = get_connection(_db_path)
    row = conn.execute(
        "SELECT consumer_appid FROM workshop_items WHERE workshop_id=?",
        (workshop_id,)
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({"success": -1, "message": "Item not found."}), 404

    appid = row["consumer_appid"]
    if not appid:
        return jsonify({"success": -1, "message": "Item has no AppID."}), 400

    login = login_secure_value(_config)
    logging.info(f"[Subscribe] POSTing to Steam: id={workshop_id}, appid={appid}, sessionid={sid[:6]}..., login={'set' if login else 'missing'}")
    try:
        resp = requests.post(
            "https://steamcommunity.com/sharedfiles/subscribe",
            data={
                "id": str(workshop_id),
                "appid": str(appid),
                "include_dependencies": "false",
                "sessionid": sid,
            },
            cookies={
                "sessionid": sid,
                "steamLoginSecure": login,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://steamcommunity.com",
                "Referer": f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            },
            timeout=15,
        )
        data = resp.json()
        logging.info(f"[Subscribe] Steam response: status={resp.status_code}, body={data}")
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
    clear_subscription_queue_status(_db_path, workshop_id)
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
