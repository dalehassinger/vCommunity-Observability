/* Scheduled prompts page: the form (sources pick, model list, schedule fields), save,
   test email / webhook, and the list (run now, on/off, history, delete). */
(function () {
  'use strict';
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function say(node, text, ok) { if (!node) return; node.textContent = text || ''; node.className = 'sched-msg ' + (ok ? 'ok' : 'err'); }

  // -- form ----------------------------------------------------------------------
  var form = $('#sched-form');
  if (form) {
    // Sources pick: the rules are in static/scope.js (the same as the chat).
    var scope = AskScope.bind($('#f-scope'), { label: $('#f-scope-label') });
    function picked() { return scope.picked(); }
    scope.refresh(false);

    // Schedule fields: show the ones the frequency needs.
    var freq = form.elements.frequency;
    function showFreq() {
      $$('[data-show]', form).forEach(function (s) { s.hidden = s.dataset.show !== freq.value; });
    }
    freq.addEventListener('change', showFreq);
    showFreq();

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

    function values() {
      var f = form.elements;
      return {
        id: f.id.value || null, name: f.name.value, prompt: f.prompt.value, scope: picked().join(',') || 'all',
        model: f.model.value, frequency: f.frequency.value, weekday: f.weekday.value, monthday: f.monthday.value,
        every_hours: f.every_hours.value, at_time: f.at_time.value, timezone: f.timezone.value,
        recipients: f.recipients.value, sync_first: f.sync_first.checked, attach_report: f.attach_report.checked,
        only_changed: f.only_changed.checked, enabled: f.enabled.checked,
        include_prompt: f.include_prompt.checked, include_sources: f.include_sources.checked,
        avoid_repeats: f.avoid_repeats.checked, send_email: f.send_email.checked, send_webhook: f.send_webhook.checked,
        webhook_name: f.webhook_name ? f.webhook_name.value : '', homeview: f.homeview.checked
      };
    }
    form.addEventListener('submit', async function (e) {
      e.preventDefault();
      $$('[data-err]', form).forEach(function (n) { n.textContent = ''; });
      var msg = $('#f-msg');
      say(msg, 'Saving…', true);
      var res = await fetch('/ui/schedules/save', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-API-Key': AskAdmin.token() },
        body: JSON.stringify(values())
      });
      var data = {};
      try { data = await res.json(); } catch (err) {}
      if (res.ok) { location.href = '/schedules'; return; }
      if (data.errors) {
        Object.keys(data.errors).forEach(function (k) {
          var n = $('[data-err="' + k + '"]', form);
          if (n) n.textContent = data.errors[k];
        });
        say(msg, 'Fix the marked fields.', false);
      } else {
        say(msg, data.detail || res.statusText, false);
      }
    });
    // Send to: the email fields show only with Email on, the webhook test only with Webhook on.
    function showSendTo() {
      $$('[data-email]', form).forEach(function (n) { n.hidden = !form.elements.send_email.checked; });
      $$('[data-hook]', form).forEach(function (n) { n.hidden = !form.elements.send_webhook.checked; });
    }
    form.elements.send_email.addEventListener('change', showSendTo);
    form.elements.send_webhook.addEventListener('change', showSendTo);
    showSendTo();
    $('#f-test-webhook').addEventListener('click', async function () {
      var m = $('#f-hook-msg');
      say(m, 'Sending…', true);
      try {
        var r = await AskAdmin.post('/ui/schedules/test-webhook', { webhook_name: form.elements.webhook_name ? form.elements.webhook_name.value : '' });
        say(m, r.result || 'Sent.', true);
      } catch (err) { say(m, err.message, false); }
    });
    $('#f-test-email').addEventListener('click', async function () {
      var m = $('#f-test-msg');
      say(m, 'Sending…', true);
      try {
        var r = await AskAdmin.post('/ui/schedules/test-email', { recipients: form.elements.recipients.value });
        say(m, r.result || 'Sent.', true);
      } catch (err) { say(m, err.message, false); }
    });
  }

  // -- list ----------------------------------------------------------------------
  // Clicks and switches in the list are handled once on the card (delegated), so they keep
  // working after a row is swapped in place by AskAdmin.refresh (no page reloads).
  var list = $('#sched-list');
  function rowSel(id) { return '#sched-list tr[data-id="' + CSS.escape(String(id)) + '"]'; }
  if (list) list.addEventListener('change', async function (e) {
    var i = e.target.closest('[data-enable]');
    if (!i) return;
    try {
      await AskAdmin.post('/ui/schedules/' + i.dataset.enable + '/enabled', { on: i.checked });
      AskAdmin.refresh(rowSel(i.dataset.enable));   // the row's "Next" time changes
    }
    catch (err) { i.checked = !i.checked; vcoAlert(err.message); }
  });
  var LABELS = { ok: 'sent', unchanged: 'no change', not_sent: 'not sent', error: 'failed', running: 'running' };
  if (list) list.addEventListener('click', async function (e) {
    var b = e.target.closest('[data-run],[data-del],[data-history]');
    if (!b) return;
    if (b.dataset.run) {
      b.disabled = true;
      try {
        await AskAdmin.post('/ui/schedules/' + b.dataset.run + '/run');
        var cell = b.closest('tr').querySelector('.sched-last');
        cell.innerHTML = '<span class="badge badge-info">running…</span>';
        watch();
      } catch (err) { b.disabled = false; vcoAlert(err.message); }
      return;
    }
    if (b.dataset.del) {
      if (!(await vcoConfirm('Delete the scheduled prompt "' + b.dataset.name + '" and its run history? The chats it made are kept.',
        { ok: 'Delete', danger: true }))) return;
      try {
        await AskAdmin.post('/ui/schedules/' + b.dataset.del + '/delete');
        var tr = b.closest('tr'), hist = document.getElementById('hist-' + b.dataset.del);
        if (hist) hist.parentNode.removeChild(hist);
        tr.parentNode.removeChild(tr);
        if (!list.querySelector('tr[data-id]')) AskAdmin.refresh('#sched-list');   // the empty card
      }
      catch (err) { vcoAlert(err.message); }
      return;
    }
    if (b.dataset.history) {   // History: the newest runs, in a row under the prompt
      var row = document.getElementById('hist-' + b.dataset.history);
      if (!row.hidden) { row.hidden = true; b.setAttribute('aria-expanded', 'false'); return; }
      var cell = row.firstElementChild;
      cell.textContent = 'Loading…';
      row.hidden = false; b.setAttribute('aria-expanded', 'true');
      var d = await (await fetch('/ui/schedules/' + b.dataset.history + '/runs', { cache: 'no-store' })).json();
      cell.textContent = '';
      if (!d.items.length) { cell.appendChild(el('span', 'subtle', 'No runs yet.')); return; }
      var t = el('table', 'datagrid sched-runs');
      t.innerHTML = '<thead><tr><th>Started</th><th>How</th><th>Result</th><th class="num">Seconds</th><th>Sent</th><th></th></tr></thead>';
      var tb = el('tbody');
      d.items.forEach(function (r) {
        var tr = el('tr');
        tr.appendChild(el('td', null, new Date(r.started * 1000).toLocaleString()));
        tr.appendChild(el('td', null, r.trigger === 'manual' ? 'Run now' : 'schedule'));
        var res = el('td'); res.appendChild(el('span', 'label ' + (r.status === 'ok' ? 'label-ok' : r.status === 'error' ? 'label-crit' : 'label-warn'), LABELS[r.status] || r.status));
        if (r.error) res.appendChild(el('div', 'sched-err', r.error));
        tr.appendChild(res);
        tr.appendChild(el('td', 'num', r.seconds != null ? String(Math.round(r.seconds)) : ''));
        tr.appendChild(el('td', 'sched-meta', r.email || ''));
        var link = el('td');
        if (r.chat_id) { var a = el('a', null, 'answer'); a.href = '/chat/' + r.chat_id; link.appendChild(a); }
        tr.appendChild(link);
        tb.appendChild(tr);
      });
      t.appendChild(tb);
      var wrap = el('div', 'table-wrap'); wrap.appendChild(t);
      cell.appendChild(wrap);
    }
  });

  // While something runs, update the rows when it ends (in place, so an open form keeps its changes).
  var timer = 0;
  function watch() {
    if (timer) return;
    timer = setInterval(async function () {
      if (document.hidden) return;
      try {
        var d = await (await fetch('/ui/schedules', { cache: 'no-store' })).json();
        if (!d.items.some(function (s) { return s.running; })) {
          clearInterval(timer); timer = 0;
          var ids = Array.prototype.map.call(document.querySelectorAll('.sched-last .badge-info'), function (x) { return x.closest('tr').dataset.id; });
          if (ids.length) AskAdmin.refresh(ids.map(rowSel));
        }
      } catch (e) {}
    }, 3000);
  }
  if (document.querySelector('.sched-last .badge-info')) watch();
})();
