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
    var scope = $('#f-scope'), label = $('#f-scope-label'), allBtn = $('[data-scope-all]', scope);
    function boxes() { return $$('input[type=checkbox]', scope); }
    function picked() { return boxes().filter(function (b) { return b.checked; }).map(function (b) { return b.value; }); }
    function showScope() {
      var p = picked();
      allBtn.setAttribute('aria-pressed', p.length ? 'false' : 'true');
      boxes().forEach(function (b) { b.closest('.scope-chip').classList.toggle('on', b.checked); });
      if (!p.length) { label.textContent = 'All sources (not spreadsheets)'; return; }
      label.textContent = p.indexOf('none') >= 0 ? 'No sources (just the model)'
        : boxes().filter(function (b) { return b.checked; }).map(function (b) { return b.nextElementSibling.textContent; }).join(', ');
    }
    scope.addEventListener('change', function (e) {
      var b = e.target;
      if (b.value === 'none' && b.checked) boxes().forEach(function (x) { if (x !== b) x.checked = false; });
      else if (b.checked) boxes().forEach(function (x) { if (x.value === 'none') x.checked = false; });
      showScope();
    });
    allBtn.addEventListener('click', function () { boxes().forEach(function (b) { b.checked = false; }); showScope(); });
    $$('[data-scope-only]', scope).forEach(function (btn) {
      btn.addEventListener('click', function () {
        boxes().forEach(function (b) { b.checked = b.dataset.plugin === btn.dataset.scopeOnly; });
        showScope();
      });
    });
    showScope();

    // Schedule fields: show the ones the frequency needs.
    var freq = form.elements.frequency;
    function showFreq() {
      $$('[data-show]', form).forEach(function (s) { s.hidden = s.dataset.show !== freq.value; });
    }
    freq.addEventListener('change', showFreq);
    showFreq();

    // Chat models from Ollama.
    var model = $('#f-model');
    fetch('/ui/ollama', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (d) {
      var cur = model.dataset.current, found = !cur;
      (d.models || []).filter(function (m) { return m.kind === 'chat'; }).forEach(function (m) {
        var o = el('option', null, m.name + ((m.capabilities || []).indexOf('tools') < 0 ? ' (no tools)' : ''));
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
        avoid_repeats: f.avoid_repeats.checked, send_email: f.send_email.checked, send_webhook: f.send_webhook.checked
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
        var r = await AskAdmin.post('/ui/schedules/test-webhook', {});
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
  $$('[data-run]').forEach(function (b) {
    b.addEventListener('click', async function () {
      b.disabled = true;
      try {
        await AskAdmin.post('/ui/schedules/' + b.dataset.run + '/run');
        var cell = b.closest('tr').querySelector('.sched-last');
        cell.innerHTML = '<span class="badge badge-info">running…</span>';
        watch();
      } catch (err) { b.disabled = false; vcoAlert(err.message); }
    });
  });
  $$('[data-enable]').forEach(function (i) {
    i.addEventListener('change', async function () {
      try { await AskAdmin.post('/ui/schedules/' + i.dataset.enable + '/enabled', { on: i.checked }); location.reload(); }
      catch (err) { i.checked = !i.checked; vcoAlert(err.message); }
    });
  });
  $$('[data-del]').forEach(function (b) {
    b.addEventListener('click', async function () {
      if (!(await vcoConfirm('Delete the scheduled prompt "' + b.dataset.name + '" and its run history? The chats it made are kept.',
        { ok: 'Delete', danger: true }))) return;
      try { await AskAdmin.post('/ui/schedules/' + b.dataset.del + '/delete'); location.reload(); }
      catch (err) { vcoAlert(err.message); }
    });
  });
  var LABELS = { ok: 'sent', unchanged: 'no change', email_failed: 'not sent', error: 'failed', running: 'running' };
  $$('[data-history]').forEach(function (b) {
    b.addEventListener('click', async function () {
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
    });
  });

  // While something runs, refresh the list when it ends.
  var timer = 0;
  function watch() {
    if (timer) return;
    timer = setInterval(async function () {
      try {
        var d = await (await fetch('/ui/schedules', { cache: 'no-store' })).json();
        if (!d.items.some(function (s) { return s.running; })) {
          clearInterval(timer); timer = 0;
          // Don't reload over an open form with unsaved changes.
          if (!form) location.reload();
          else document.querySelectorAll('.sched-last .badge-info').forEach(function (b) { b.textContent = 'finished: reload to see'; });
        }
      } catch (e) {}
    }, 3000);
  }
  if (document.querySelector('.sched-last .badge-info')) watch();
})();
