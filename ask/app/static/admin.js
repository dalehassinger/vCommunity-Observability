/* Shared helpers for the Sources and Settings pages: API token bar and JSON POSTs.
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
  document.addEventListener('DOMContentLoaded', init);
  return { post: post, token: token };
})();
