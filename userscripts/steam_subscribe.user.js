// ==UserScript==
// @name         Steam Workshop Scraper — Subscribe Bridge
// @namespace    https://github.com/MyrddinE/steam-workshop-scraper
// @version      3
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
    return;
  }

  // ── Inject subscribe bridge into the scraper web UI ──────────────────
  if (!isScraper) return;

  const API_BASE = location.origin;

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

  // Listen for subscribe requests from the page and proxy via GM_xmlhttpRequest
  window.addEventListener('message', function (e) {
    if (e.source !== window || !e.data || e.data._ss !== 'sub') return;
    const { id, appid, seq } = e.data;
    const csrf = getSessionId();
    GM_xmlhttpRequest({
      method: 'POST',
      url: 'https://steamcommunity.com/sharedfiles/subscribe',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      data: `id=${encodeURIComponent(id)}&appid=${encodeURIComponent(appid)}&include_dependencies=false&sessionid=${encodeURIComponent(csrf)}`,
      onload: function (resp) {
        try {
          window.postMessage({ _ss: 'res', seq, result: JSON.parse(resp.responseText) }, '*');
        } catch (e) {
          window.postMessage({ _ss: 'res', seq, result: { success: -1, message: 'Parse error' } }, '*');
        }
      },
      onerror: function () {
        window.postMessage({ _ss: 'res', seq, result: { success: -1, message: 'Network error' } }, '*');
      },
      ontimeout: function () {
        window.postMessage({ _ss: 'res', seq, result: { success: -1, message: 'Steam request timed out' } }, '*');
      },
    });
  });

})();
