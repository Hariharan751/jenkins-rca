// Jenkins RCA — theme toggle (light/dark) with localStorage + system pref.
// Loads BEFORE first paint via inline bootstrap in <head>; this script
// installs the toggle button click handler after DOM is ready.
(function () {
  'use strict';

  var STORAGE_KEY = 'jenkins-rca-theme';

  function getStoredTheme() {
    try { return localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
  }

  function setStoredTheme(t) {
    try { localStorage.setItem(STORAGE_KEY, t); } catch (e) { /* noop */ }
  }

  function systemPrefersDark() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  }

  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
  }

  // Resolve current theme: explicit user choice > system preference > dark.
  function resolveTheme() {
    var stored = getStoredTheme();
    if (stored === 'light' || stored === 'dark') return stored;
    return systemPrefersDark() ? 'dark' : 'light';
  }

  // Initial apply (early — before paint when called from inline bootstrap).
  applyTheme(resolveTheme());

  // Wire up toggle button after DOM ready.
  function installToggle() {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    btn.addEventListener('click', function () {
      var current = document.documentElement.getAttribute('data-theme') || 'dark';
      var next = current === 'dark' ? 'light' : 'dark';
      applyTheme(next);
      setStoredTheme(next);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installToggle);
  } else {
    installToggle();
  }

  // React to system pref changes when user hasn't explicitly picked.
  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    mq.addEventListener('change', function () {
      if (!getStoredTheme()) applyTheme(resolveTheme());
    });
  }
})();
