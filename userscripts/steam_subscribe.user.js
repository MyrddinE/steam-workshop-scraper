// ==UserScript==
// @name         Steam Workshop Scraper — Subscribe Bridge
// @namespace    https://github.com/MyrddinE/steam-workshop-scraper
// @version      1.0
// @description  Bridges Steam session to the Workshop Scraper web UI for one-click subscribing.
// @author       MyrddinE
// @match        https://steamcommunity.com/*
// @match        http://localhost:8080/*
// @match        http://127.0.0.1:8080/*
// @match        http://localhost:8081/*
// @match        http://127.0.0.1:8081/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @run-at       document-end
// ==/UserScript==

(function () {
  'use strict';

  const isSteam = location.hostname === 'steamcommunity.com';
  const isScraper = location.hostname === 'localhost' || location.hostname === '127.0.0.1';

  // ── Capture sessionid from Steam ─────────────────────────────────────
  if (isSteam) {
    function captureSession() {
      const match = document.cookie.match(/(?:^|;\s*)sessionid=([0-9a-f]+)/);
      if (match && match[1]) {
        const sid = match[1];
        const prev = GM_getValue('steam_sessionid', '');
        if (sid !== prev) {
          GM_setValue('steam_sessionid', sid);
          console.log('[SubscribeBridge] sessionid captured:', sid.slice(0, 6) + '...');
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

  function getSessionId() {
    return GM_getValue('steam_sessionid', '');
  }

  async function steamSubscribe(id, appid) {
    const sessionid = getSessionId();
    if (!sessionid || !sessionid.trim()) {
      return { success: -1, message: 'No Steam session found. Please visit steamcommunity.com in this browser and log in.' };
    }

    return new Promise((resolve) => {
      GM_xmlhttpRequest({
        method: 'POST',
        url: 'https://steamcommunity.com/sharedfiles/subscribe',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        data: `id=${encodeURIComponent(id)}&appid=${encodeURIComponent(appid)}&include_dependencies=false&sessionid=${encodeURIComponent(sessionid)}`,
        onload: function (resp) {
          try {
            const data = JSON.parse(resp.responseText);
            resolve(data);
          } catch (e) {
            resolve({ success: -1, message: 'Failed to parse Steam response.' });
          }
        },
        onerror: function () {
          resolve({ success: -1, message: 'Network error contacting Steam.' });
        },
        ontimeout: function () {
          resolve({ success: -1, message: 'Steam request timed out.' });
        },
      });
    });
  }

  // Inject the function into the page scope
  unsafeWindow.steamSubscribe = steamSubscribe;
  console.log('[SubscribeBridge] steamSubscribe() injected into page');

  // Also send sessionid to the backend so the TUI can use it
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
        // Backend might not be running yet — retry later
        setTimeout(pushSessionToBackend, 5000);
      },
    });
  }
  pushSessionToBackend();

})();
