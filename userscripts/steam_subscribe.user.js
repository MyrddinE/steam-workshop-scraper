// ==UserScript==
// @name         Steam Workshop Scraper — Subscribe Bridge
// @namespace    https://github.com/MyrddinE/steam-workshop-scraper
// @version      5
// @description  Bridges Steam session to the Workshop Scraper web UI for one-click subscribing.
// @author       MyrddinE
// @match        https://steamcommunity.com/*
// @include      http://localhost:*/
// @include      http://127.0.0.1:*/
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_info
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
    function captureSession() {
      const sidMatch = document.cookie.match(/(?:^|;\s*)sessionid=([0-9a-f]+)/);
      const loginMatch = document.cookie.match(/(?:^|;\s*)steamLoginSecure=([^;]+)/);
      if (sidMatch && sidMatch[1]) {
        const sid = sidMatch[1];
        const login = loginMatch ? loginMatch[1] : '';
        const prevSid = GM_getValue('steam_sessionid', '');
        const prevLogin = GM_getValue('steam_login_secure', '');
        if (sid !== prevSid || login !== prevLogin) {
          GM_setValue('steam_sessionid', sid);
          if (login) GM_setValue('steam_login_secure', login);
          const short = sid.slice(0, 6) + '...';
          console.log('[SubscribeBridge] sessionid captured:', short, login ? '(+ login)' : '');
          showToast('New session captured: ' + short + ' — reload the Scraper web UI');
        }
      }
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

      setTimeout(function () {
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
              } else if (Date.now() - startTime > maxWait) {
                clearInterval(poll);
                reportAndClose(apiBase, wid);
              }
            }, 500);
          }
        } else {
          reportAndClose(apiBase, wid);
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

  // Push sessionid to the backend so the TUI / server can subscribe
  function pushSessionToBackend() {
    const sid = getSessionId();
    if (!sid) return;
    GM_xmlhttpRequest({
      method: 'POST',
      url: API_BASE + '/api/sessionid',
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ sessionid: sid }),
      onload: function () {
        console.log('[SubscribeBridge] sessionid pushed to backend');
      },
      onerror: function () {
        setTimeout(pushSessionToBackend, 5000);
      },
    });
  }
  pushSessionToBackend();
  setInterval(pushSessionToBackend, 30000);

})();
