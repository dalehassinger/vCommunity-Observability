/* Shared helpers for the admin pages (Sources, Settings, Saved and Scheduled prompts, Reports):
   API token bar, JSON POSTs and refresh() (update parts of the page in place).
   The token is kept in this browser only (localStorage) and sent as X-API-Key. */
window.AskAdmin = (function () {
  'use strict';
  var KEY = 'ask-token';
  function token() {
    var el = document.getElementById('api-token');
    if (el && el.value) return el.value;
    try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; }
  }
  function init() {
    var el = document.getElementById('api-token');
    if (!el) return;
    try { el.value = localStorage.getItem(KEY) || ''; } catch (e) {}
    el.addEventListener('change', function () { try { localStorage.setItem(KEY, el.value); } catch (e) {} });
  }
  async function post(url, body) {
    var res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': token() },
      body: JSON.stringify(body || {})
    });
    var data = {};
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) {
      var msg = data.detail || (data.errors ? Object.keys(data.errors).map(function (k) { return k + ': ' + data.errors[k]; }).join(', ') : res.statusText);
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return data;
  }
  // A selector that finds the focused control again after its part of the page is replaced.
  function focusKey(node) {
    if (!node || node === document.body) return null;
    if (node.id) return '#' + CSS.escape(node.id);
    for (var i = 0; i < node.attributes.length; i++) {
      var a = node.attributes[i];
      if (a.name.indexOf('data-') === 0) return node.tagName.toLowerCase() + '[' + a.name + '="' + CSS.escape(a.value) + '"]';
    }
    return null;
  }
  // Update parts of the page in place instead of location.reload(): fetch this page's HTML again
  // and swap only the elements the selectors name. Style sheets and scripts are not loaded again,
  // and the scroll position and the focused control stay. Event handlers must be delegated from
  // an element that is not swapped. When a part is missing on either side (the page changed
  // shape, for example the first item was added) the page is reloaded as before.
  async function refresh(selectors) {
    if (typeof selectors === 'string') selectors = [selectors];
    var res = await fetch(location.pathname + location.search, { cache: 'no-store', headers: { 'Accept': 'text/html' } });
    if (!res.ok) { location.reload(); return false; }
    var doc = new DOMParser().parseFromString(await res.text(), 'text/html');
    var pairs = [];
    for (var i = 0; i < selectors.length; i++) {
      var olds = document.querySelectorAll(selectors[i]), news = doc.querySelectorAll(selectors[i]);
      if (!olds.length || olds.length !== news.length) { location.reload(); return false; }
      for (var k = 0; k < olds.length; k++) pairs.push([olds[k], news[k]]);
    }
    var x = window.scrollX, y = window.scrollY, active = document.activeElement, key = null;
    if (pairs.some(function (p) { return p[0].contains(active); })) key = focusKey(active);
    pairs.forEach(function (p) { p[0].replaceWith(document.adoptNode(p[1])); });
    if (key) { var again = document.querySelector(key); if (again) again.focus({ preventScroll: true }); }
    window.scrollTo(x, y);
    document.dispatchEvent(new CustomEvent('ask:refreshed', { detail: { selectors: selectors } }));
    return true;
  }
  document.addEventListener('DOMContentLoaded', init);
  return { post: post, token: token, refresh: refresh };
})();
