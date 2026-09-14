// ==UserScript==
// @name         Steam Workshop Scraper — Subscribe Bridge
// @namespace    https://github.com/MyrddinE/steam-workshop-scraper
// @version      10
// @updateURL    https://raw.githubusercontent.com/MyrddinE/steam-workshop-scraper/main/userscripts/steam_subscribe.user.js
// @downloadURL  https://raw.githubusercontent.com/MyrddinE/steam-workshop-scraper/main/userscripts/steam_subscribe.user.js
// @description  Bridges Steam session to the Workshop Scraper web UI for one-click subscribing.
// @author       MyrddinE
// @match        https://steamcommunity.com/*
// @include      http://localhost:*/
// @include      http://127.0.0.1:*/
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_info
// @grant        GM_cookie
// @run-at       document-end
// ==/UserScript==

(function () {
  'use strict';

  const isSteam = location.hostname === 'steamcommunity.com';
  const isScraper = !!document.querySelector('meta[name="userscript-version"]');

  // ── Capture sessionid from Steam ─────────────────────────────────────
  if (isSteam) {
    function showToast(msg) {
      const t = document.createElement('div');
      t.style.cssText = 'position:fixed;top:8px;right:8px;z-index:999999;' +
        'background:#1a6d1a;color:#fff;padding:8px 16px;border-radius:4px;' +
        'font:13px Arial;box-shadow:0 2px 8px rgba(0,0,0,.4);pointer-events:none;';
      t.textContent = '[SubscribeBridge] ' + msg;
      document.body.appendChild(t);
      setTimeout(function(){ t.remove(); }, 6000);
    }
    // Steam marks steamLoginSecure HttpOnly, so document.cookie can never contain
    // it — only GM_cookie can, and Tampermonkey supports HttpOnly through that API
    // on BETA builds. The document.cookie path stays as a fallback, so a stable
    // build still captures sessionid and behaves exactly as it did before.
    function cookieApi() {
      if (typeof GM_cookie !== 'undefined' && GM_cookie && GM_cookie.list) return GM_cookie;
      if (typeof GM !== 'undefined' && GM && GM.cookie && GM.cookie.list) return GM.cookie;
      return null;
    }

    function cookiesFromDocument() {
      const found = {};
      const sid = document.cookie.match(/(?:^|;\s*)sessionid=([^;]+)/);
      if (sid) found.sessionid = sid[1];
      const login = document.cookie.match(/(?:^|;\s*)steamLoginSecure=([^;]+)/);
      if (login) found.steamLoginSecure = login[1];
      return found;
    }

    function storeSession(found) {
      const sid = found.sessionid;
      if (!sid) return;
      const login = found.steamLoginSecure || '';
      const prevSid = GM_getValue('steam_sessionid', '');
      const prevLogin = GM_getValue('steam_login_secure', '');
      if (sid === prevSid && (!login || login === prevLogin)) return;
      GM_setValue('steam_sessionid', sid);
      if (login) GM_setValue('steam_login_secure', login);
      const short = sid.slice(0, 6) + '...';
      console.log('[SubscribeBridge] session captured:', short,
                  login ? '(+ login cookie)' : '(login cookie NOT captured)');
      showToast('New session captured: ' + short + ' — reload the Scraper web UI');
    }

    function captureSession() {
      const api = cookieApi();
      if (!api) {
        storeSession(cookiesFromDocument());
        return;
      }
      api.list({ url: 'https://steamcommunity.com/' }, function (cookies, error) {
        if (error || !cookies) {
          console.warn('[SubscribeBridge] GM_cookie.list failed (' + error +
                       '); falling back to document.cookie');
          storeSession(cookiesFromDocument());
          return;
        }
        const found = {};
        cookies.forEach(function (c) { found[c.name] = c.value; });
        storeSession(found);
      });
    }
    captureSession();
    // Re-capture after a few seconds in case the cookie loads late
    setTimeout(captureSession, 3000);

    // ── Auto-subscribe triggered by the scraper web UI ──────────────
    if (location.search.includes('autosubscribe=true')) {
      var apiBase = GM_getValue('api_base', '') || new URLSearchParams(location.search).get('callback_origin') || '';
      var wid = new URLSearchParams(location.search).get('id');

      function reportAndClose(apiBase, wid) {
        if (apiBase && wid) {
          var url = apiBase + '/api/subscribed/' + wid;
          GM_xmlhttpRequest({
            method: 'POST',
            url: url,
            headers: { 'Content-Type': 'application/json' },
            onload: function () {
              console.log('[SubscribeBridge] Verified subscription reported for', wid);
              setTimeout(function () { window.close(); }, 500);
            },
            onerror: function () {
              console.error('[SubscribeBridge] Failed to report for', wid, '- retrying once');
              setTimeout(function () {
                GM_xmlhttpRequest({
                  method: 'POST',
                  url: url,
                  headers: { 'Content-Type': 'application/json' },
                });
                setTimeout(function () { window.close(); }, 500);
              }, 2000);
            },
          });
        } else {
          setTimeout(function () { window.close(); }, 500);
        }
      }

      function isVerified() {
        var btn = document.getElementById('SubscribeItemBtn');
        if (btn && btn.classList.contains('toggled')) return true;
        var tips = document.querySelectorAll('.subscribeResult, .general_tip, .infoText');
        for (var i = 0; i < tips.length; i++) {
          if (tips[i].textContent.indexOf('This item has been added to your Subscription') !== -1) return true;
        }
        return false;
      }

      // Steam answers an over-budget request with HTTP 200 and its ordinary page
      // shell carrying this wording, and the subscribe button is simply absent —
      // so without this check a throttled item was indistinguishable from one
      // that failed, and got cleared from the queue as a failure.
      function isThrottled() {
        var text = (document.body && document.body.innerText || '').toLowerCase();
        return text.indexOf('too many requests') !== -1;
      }

      function reportThrottledAndClose(apiBase, wid) {
        if (apiBase && wid) {
          GM_xmlhttpRequest({
            method: 'POST',
            url: apiBase + '/api/subscribe_throttled/' + wid,
            headers: { 'Content-Type': 'application/json' },
            onload: function () {
              console.log('[SubscribeBridge] Reported throttling for', wid, '- left queued');
              setTimeout(function () { window.close(); }, 500);
            },
            onerror: function () {
              setTimeout(function () { window.close(); }, 500);
            },
          });
        } else {
          setTimeout(function () { window.close(); }, 500);
        }
      }

      function reportFailureAndClose(apiBase, wid) {
        if (apiBase && wid) {
          var url = apiBase + '/api/subscribe_failed/' + wid;
          GM_xmlhttpRequest({
            method: 'POST',
            url: url,
            headers: { 'Content-Type': 'application/json' },
            onload: function () {
              console.log('[SubscribeBridge] Reported subscription failure for', wid);
              setTimeout(function () { window.close(); }, 500);
            },
            onerror: function () {
              setTimeout(function () { window.close(); }, 500);
            },
          });
        } else {
          setTimeout(function () { window.close(); }, 500);
        }
      }

      setTimeout(function () {
        if (isThrottled()) {
          showToast('Steam is throttling - leaving this one queued');
          reportThrottledAndClose(apiBase, wid);
          return;
        }
        var btn = document.getElementById('SubscribeItemBtn');
        if (btn) {
          if (btn.classList.contains('toggled')) {
            showToast('Already subscribed');
            reportAndClose(apiBase, wid);
          } else {
            btn.click();
            showToast('Subscribing...');

            var startTime = Date.now();
            var maxWait = 12000;
            var poll = setInterval(function () {
              if (isVerified()) {
                clearInterval(poll);
                showToast('Subscribed!');
                reportAndClose(apiBase, wid);
              } else if (isThrottled()) {
                // Checked before the timeout claim: reporting this as a success
                // or a failure would both be wrong, since nothing was attempted.
                clearInterval(poll);
                showToast('Steam is throttling - leaving this one queued');
                reportThrottledAndClose(apiBase, wid);
              } else if (Date.now() - startTime > maxWait) {
                clearInterval(poll);
                reportAndClose(apiBase, wid);
              }
            }, 500);
          }
        } else {
          reportFailureAndClose(apiBase, wid);
        }
      }, 2000);
    }
    return;
  }

  // ── Inject subscribe bridge into the scraper web UI ──────────────────
  if (!isScraper) return;

  const API_BASE = location.origin;

  // Store for autosubscribe tabs (they run on Steam, not the scraper page)
  GM_setValue('api_base', API_BASE);

  // ── Version check ─────────────────────────────────────────────────
  const EXPECTED_VER = parseInt(
    document.querySelector('meta[name="userscript-version"]')?.content || '0',
    10
  );
  const CURRENT_VER = parseInt((GM_info?.script?.version || '0'), 10);
  if (EXPECTED_VER > CURRENT_VER) {
    const msg = `[SubscribeBridge] This userscript is out of date (v${CURRENT_VER}). ` +
      `The page expects v${EXPECTED_VER}. Please update from the install URL.`;
    console.warn(msg);
    alert(msg);
    return;  // Prevent injection of outdated bridge
  }

  // ── Stamp the DOM so the page knows we're here ────────────────────
  document.body.dataset.userscript = '1';
  document.body.dataset.userscriptVer = String(CURRENT_VER);

  function getSessionId() {
    return GM_getValue('steam_sessionid', '');
  }

  function getLoginSecure() {
    return GM_getValue('steam_login_secure', '');
  }

  const sid = getSessionId();
  console.log(`[SubscribeBridge] v${CURRENT_VER} active, sessionid ${sid ? sid.slice(0,6)+'...' : '✗'}`);

  // Push sessionid and the login cookie to the backend so the TUI / server can
  // subscribe. login_secure was captured but never sent before, so the server
  // logged "login_secure: missing" even once it had been read.
  //
  // Sent on load, then re-checked every 30s but only when the values differ from
  // the last successful push. A cookie is valid for days, so re-sending an
  // unchanged one was rewriting config.yaml and logging a line every half minute
  // per open Steam tab, for nothing. The slow re-push below is the one case that
  // does need it: a backend that restarted has lost its in-memory session, and
  // this is what re-syncs it without waiting for the user to reload the UI.
  const REPUSH_AFTER_MS = 10 * 60 * 1000;
  // A failed push used to re-arm itself every five seconds forever, so a
  // backend that was down was polled for as long as a Steam tab stayed open,
  // and nothing ever gave up. Consecutive failures now double the delay from
  // five seconds up to a one-minute cap and stop after six retries. A success
  // resets the counter, so a later transient failure retries promptly again.
  const RETRY_INITIAL_MS = 5000;
  const RETRY_MAX_MS = 60000;
  const RETRY_MAX_ATTEMPTS = 6;
  let lastPushed = null;
  let lastPushedAt = 0;
  let retryTimer = null;
  let retryDelay = RETRY_INITIAL_MS;
  let retriesLeft = RETRY_MAX_ATTEMPTS;

  function scheduleRetry() {
    // One chain at a time: a timer already pending owns the next attempt.
    if (retryTimer !== null || retriesLeft <= 0) return;
    const delay = retryDelay;
    retriesLeft -= 1;
    retryDelay = Math.min(retryDelay * 2, RETRY_MAX_MS);
    retryTimer = setTimeout(function () {
      retryTimer = null;
      pushSessionToBackend();
    }, delay);
  }

  function pushSessionToBackend() {
    // The pending retry covers the next attempt, so a periodic tick in the
    // meantime must not start a second, parallel stream of requests.
    if (retryTimer !== null) return;
    const sid = getSessionId();
    if (!sid) return;
    const login = getLoginSecure();
    const payload = sid + '\n' + login;
    if (payload === lastPushed && Date.now() - lastPushedAt < REPUSH_AFTER_MS) return;
    GM_xmlhttpRequest({
      method: 'POST',
      url: API_BASE + '/api/sessionid',
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ sessionid: sid, login_secure: login }),
      onload: function () {
        lastPushed = payload;
        lastPushedAt = Date.now();
        retryDelay = RETRY_INITIAL_MS;
        retriesLeft = RETRY_MAX_ATTEMPTS;
        if (retryTimer !== null) { clearTimeout(retryTimer); retryTimer = null; }
        console.debug('[SubscribeBridge] sessionid pushed to backend' +
                      (login ? ' (with login cookie)' : ' (no login cookie)'));
      },
      onerror: function () {
        scheduleRetry();
      },
    });
  }
  pushSessionToBackend();
  setInterval(pushSessionToBackend, 30000);

})();
