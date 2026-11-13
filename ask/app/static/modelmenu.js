/* vCommunity Ask chat page: the chat model button under the prompt is a menu. Opening it lists the chat models
   installed in Ollama (GET /ui/ollama); picking one saves the Chat model setting
   (POST /ui/settings, same as the Settings page) and applies from the next question.
   With ASK_API_TOKEN set, the token entered on Settings or Sources (kept in this
   browser) is sent; without it the save is refused and the menu says so. */
(function () {
  'use strict';
  var box = document.getElementById('model-menu');
  if (!box) return;
  var list = box.querySelector('.model-list'), label = box.querySelector('.model-current'), loaded = false;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function token() { try { return localStorage.getItem('ask-token') || ''; } catch (e) { return ''; } }
  function msg(text, cls) { list.innerHTML = ''; list.appendChild(el('div', 'model-msg ' + (cls || ''), text)); }
  function same(a, b) { return a === b || a + ':latest' === b || a === b + ':latest'; }

  async function load() {
    msg('Loading models…');
    try {
      var d = await (await fetch('/ui/ollama', { cache: 'no-store' })).json();
      if (!d.ok) { msg(d.error || 'Ollama did not answer', 'err'); return; }
      var chat = d.models.filter(function (m) { return m.kind === 'chat'; });
      if (!chat.length) { msg('No chat models installed in Ollama.', 'err'); return; }
      list.innerHTML = '';
      var cur = box.dataset.current;
      chat.forEach(function (m) {
        var b = el('button', 'model-item' + (same(m.name, cur) ? ' on' : ''));
        b.type = 'button';
        b.setAttribute('role', 'menuitemradio');
        b.setAttribute('aria-checked', same(m.name, cur) ? 'true' : 'false');
        b.appendChild(el('span', 'model-name', m.name));
        var tags = [];
        if (m.parameter_size) tags.push(m.parameter_size);
        if ((m.capabilities || []).indexOf('tools') < 0) tags.push('no tools');
        if ((m.capabilities || []).indexOf('thinking') >= 0) tags.push('thinking');
        if (tags.length) b.appendChild(el('span', 'model-tags', tags.join(' · ')));
        b.title = (m.capabilities || []).indexOf('tools') < 0
          ? 'This model cannot call tools: web search and MCP tools will not work with it' : '';
        b.addEventListener('click', function () { pick(m.name); });
        list.appendChild(b);
      });
      list.appendChild(el('a', 'model-foot', 'More model settings')).href = '/settings';
      loaded = true;
    } catch (e) { msg(e.message, 'err'); }
  }

  async function pick(name) {
    if (same(name, box.dataset.current)) { box.open = false; return; }
    msg('Switching to ' + name + '…');
    try {
      var res = await fetch('/ui/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-API-Key': token() },
        body: JSON.stringify({ values: { chat_model: name } })
      });
      if (!res.ok) {
        var d = {};
        try { d = await res.json(); } catch (e) { /* not JSON */ }
        throw new Error(res.status === 401 ? 'Enter the API token on the Settings page first, then try again.'
          : (typeof d.detail === 'string' ? d.detail : res.statusText));
      }
      box.dataset.current = name;
      label.textContent = name;
      loaded = false;
      box.open = false;
      // The Settings page holds the old value in its form: reload it so a later Save keeps this one.
      if (location.pathname === '/settings') location.reload();
    } catch (e) { msg(e.message, 'err'); loaded = false; }
  }

  box.addEventListener('toggle', function () { if (box.open && !loaded) load(); });
  document.addEventListener('click', function (e) { if (box.open && !box.contains(e.target)) box.open = false; });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && box.open) { box.open = false; box.querySelector('summary').focus(); } });
})();
