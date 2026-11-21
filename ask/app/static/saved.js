/* Saved prompts page: the form (sources pick, pages to fetch, model list), save, and the
   list (Run / Edit before sending open the chat, delete one or the selected ones).
   "Save prompt" in a chat opens /saved?new=1 with the question and sources in
   sessionStorage (ask.saved.draft), which fill the new form here. */
(function () {
  'use strict';
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function say(node, text, ok) { if (!node) return; node.textContent = text || ''; node.className = 'sched-msg ' + (ok ? 'ok' : 'err'); }

  // -- form ----------------------------------------------------------------------
  var form = $('#saved-form');
  if (form) {
    // Sources pick: the rules are in static/scope.js (the same as the chat).
    var scope = AskScope.bind($('#f-scope'), { label: $('#f-scope-label') });
    function picked() { return scope.picked(); }

    // A prompt sent here from a chat ("Save prompt").
    if (!form.elements.id.value && /[?&]from=chat\b/.test(location.search)) {
      try {
        var draft = JSON.parse(sessionStorage.getItem('ask.saved.draft') || 'null');
        if (draft) {
          // A question from a saved prompt with pages: put the pages back in their own field.
          var parts = String(draft.prompt || '').split('\n\nRead these pages with web_fetch:\n');
          form.elements.prompt.value = parts[0];
          if (parts[1]) form.elements.urls.value = parts[1].split('\n').map(function (l) { return l.replace(/^- /, '').trim(); }).filter(Boolean).join('\n');
          scope.set(draft.scope);
          sessionStorage.removeItem('ask.saved.draft');
        }
      } catch (e) { /* nothing saved */ }
    }
    scope.refresh(false);

    // Chat models from Ollama and LM Studio (whichever are on).
    var model = $('#f-model');
    fetch('/ui/models', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (d) {
      var cur = model.dataset.current, found = !cur;
      (d.models || []).filter(function (m) { return m.kind === 'chat'; }).forEach(function (m) {
        var o = el('option', null, (m.label || m.name) + (m.provider === 'lmstudio' ? ' · LM Studio' : '') + ((m.capabilities || []).indexOf('tools') < 0 ? ' (no tools)' : ''));
        o.value = m.name;
        if (m.name === cur) { o.selected = true; found = true; }
        model.appendChild(o);
      });
      if (!found) { var o = el('option', null, cur + ' (not installed)'); o.value = cur; o.selected = true; model.appendChild(o); }
    }).catch(function () {});

    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      $$('[data-err]', form).forEach(function (n) { n.textContent = ''; });
      var f = form.elements, msg = $('#f-msg');
      say(msg, 'Saving…', true);
      var res = await fetch('/ui/saved/save', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-API-Key': AskAdmin.token() },
        body: JSON.stringify({ id: f.id.value || null, name: f.name.value, prompt: f.prompt.value,
                               scope: picked().join(',') || 'all', urls: f.urls.value, model: f.model.value, tags: f.tags.value })
      });
      var data = {};
      try { data = await res.json(); } catch (err) {}
      if (res.ok) { location.href = '/saved'; return; }
      if (data.errors) {
        Object.keys(data.errors).forEach(function (k) { var n = $('[data-err="' + k + '"]', form); if (n) n.textContent = data.errors[k]; });
        say(msg, 'Fix the marked fields.', false);
      } else {
        say(msg, data.detail || res.statusText, false);
      }
    });
  }

  // -- list ----------------------------------------------------------------------
  var all = $('#saved-all'), delBtn = $('#saved-del'), count = $('#saved-count');
  // Search box and tag chips: show only the rows that match (the tag pick is kept in this browser).
  var qBox = $('#saved-q'), tagBox = $('#saved-tags'), none = $('#saved-none'), tag = '';
  function rows() { return $$('#saved-list tbody tr[data-id]'); }
  function filter() {
    var words = (qBox ? qBox.value : '').toLowerCase().split(/\s+/).filter(Boolean), shown = 0;
    rows().forEach(function (r) {
      var ok = (!tag || r.dataset.tags.split(',').indexOf(tag) >= 0) &&
        words.every(function (w) { return r.dataset.text.indexOf(w) >= 0; });
      r.hidden = !ok;
      if (!ok) r.querySelector('.saved-box').checked = false;
      if (ok) shown++;
    });
    if (none) none.hidden = shown > 0 || !rows().length;
    refresh();
  }
  function setTag(t, save) {
    tagBox = $('#saved-tags');
    if (tagBox && !tagBox.querySelector('[data-tag="' + CSS.escape(t) + '"]')) t = '';
    tag = t;
    $$('.tag-chip', tagBox).forEach(function (c) { var on = c.dataset.tag === tag; c.classList.toggle('on', on); c.setAttribute('aria-pressed', on ? 'true' : 'false'); });
    if (save) { try { localStorage.setItem('ask.saved.tag', tag); } catch (e) { /* private window */ } }
    filter();
  }
  // Clicks in the list are handled once on the card (delegated), so they keep working after
  // refresh() swaps the table body or the tag chips in place.
  var listCard = $('#saved-list');
  if (listCard) listCard.addEventListener('click', function (e) { var c = e.target.closest('#saved-tags .tag-chip'); if (c) setTag(c.dataset.tag, true); });
  if (qBox) {
    qBox.addEventListener('input', filter);
    qBox.addEventListener('keydown', function (e) {
      // Enter runs the only (or first) match.
      if (e.key !== 'Enter') return;
      var first = rows().filter(function (r) { return !r.hidden; })[0];
      if (first) { e.preventDefault(); location.href = first.querySelector('.btn-run').href; }
    });
  }
  var clear = $('#saved-clear');
  if (clear) clear.addEventListener('click', function () { if (qBox) qBox.value = ''; setTag('', true); });
  function ticked() { return $$('.saved-box').filter(function (b) { return b.checked; }); }
  function refresh() {
    var n = ticked().length, total = rows().filter(function (r) { return !r.hidden; }).length;
    if (delBtn) { delBtn.disabled = !n; delBtn.textContent = n ? 'Delete selected (' + n + ')' : 'Delete selected'; }
    if (count) count.textContent = n ? n + ' of ' + total + ' selected' : '';
    if (all) { all.checked = !!n && n === total; all.indeterminate = !!n && n < total; }
  }
  var lastBox = null;
  if (listCard) listCard.addEventListener('click', function (e) {
    var b = e.target.closest('.saved-box');
    if (!b) return;
    // Shift-click ticks the range from the last box clicked.
    if (e.shiftKey && lastBox && lastBox !== b && lastBox.isConnected) {
      var list = $$('.saved-box').filter(function (x) { return !x.closest('tr').hidden; }), i = list.indexOf(lastBox), j = list.indexOf(b);
      list.slice(Math.min(i, j), Math.max(i, j) + 1).forEach(function (x) { x.checked = b.checked; });
    }
    lastBox = b;
    refresh();
  });
  if (all) all.addEventListener('change', function () { rows().forEach(function (r) { if (!r.hidden) r.querySelector('.saved-box').checked = all.checked; }); refresh(); });
  // Swap the rows and tag chips for fresh ones from the server (pin order, import), keeping the
  // search and tag filter. No page reload.
  function reloadList() {
    return AskAdmin.refresh(['#saved-list tbody', '#saved-tags']).then(function () { setTag(tag, false); });
  }
  async function remove(ids, text) {
    if (!(await vcoConfirm(text, { ok: 'Delete', danger: true }))) return;
    try {
      await AskAdmin.post('/ui/saved/delete', { ids: ids });
      // Deleted rows leave in place; the last one shows the empty card.
      rows().forEach(function (r) { if (ids.indexOf(Number(r.dataset.id)) >= 0) r.parentNode.removeChild(r); });
      if (!rows().length) { AskAdmin.refresh('#saved-list'); return; }
      filter();
    }
    catch (err) { vcoAlert(err.message); }
  }
  if (delBtn) delBtn.addEventListener('click', function () {
    var ids = ticked().map(function (b) { return Number(b.value); });
    remove(ids, 'Delete ' + ids.length + ' saved prompt' + (ids.length === 1 ? '' : 's') + '? The chats they made are kept.');
  });
  if (listCard) listCard.addEventListener('click', function (e) {
    var b = e.target.closest('[data-del]');
    if (b) remove([Number(b.dataset.del)], 'Delete the saved prompt "' + b.dataset.name + '"? The chats it made are kept.');
  });
  // Pin / unpin (★): pinned prompts list first, so the rows are fetched again in their new order.
  if (listCard) listCard.addEventListener('click', function (e) {
    var b = e.target.closest('[data-pin]');
    if (!b) return;
    fetch('/ui/saved/' + b.dataset.pin + '/pin', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                 body: JSON.stringify({ on: b.dataset.on !== '1' }) })
      .then(function (r) { if (!r.ok) throw new Error(r.statusText); return reloadList(); })
      .catch(function (err) { vcoAlert(err.message); });
  });
  // Import: a JSON file made by Export (here or in another Ask).
  var impBtn = $('#saved-import-btn'), impFile = $('#saved-import'), impMsg = $('#saved-import-msg');
  if (impBtn && impFile) {
    impBtn.addEventListener('click', function () { impFile.value = ''; impFile.click(); });
    impFile.addEventListener('change', async function () {
      var f = impFile.files[0];
      if (!f) return;
      var data;
      try { data = JSON.parse(await f.text()); } catch (e) { say(impMsg, f.name + ' is not a JSON file.', false); return; }
      try {
        var r = await AskAdmin.post('/ui/saved/import', data);
        var text = 'Imported ' + f.name + ': ' + r.added + ' added, ' + r.replaced + ' replaced' + (r.skipped.length ? ', ' + r.skipped.length + ' skipped (' + r.skipped.slice(0, 3).join('; ') + ')' : '') + '.';
        say(impMsg, text, !r.skipped.length);
        // The first prompts on an empty page change its whole layout, so that one case reloads.
        if (rows().length) reloadList(); else location.reload();
      } catch (err) { say(impMsg, err.message, false); }
    });
    try {
      var note = sessionStorage.getItem('ask.saved.imported');
      if (note) { say(impMsg, note, true); sessionStorage.removeItem('ask.saved.imported'); }
    } catch (e) { /* private window */ }
  }
  var saved0 = '';
  try { saved0 = localStorage.getItem('ask.saved.tag') || ''; } catch (e) { /* private window */ }
  setTag(saved0, false);
  if (qBox && !form) qBox.focus();
})();
