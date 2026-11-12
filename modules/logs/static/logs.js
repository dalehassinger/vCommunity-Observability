/* Logs module pages: Logs (search, chart, live tail), Log alerts (rules, fired alerts,
   notifications) and Log settings (retention, listener, senders, setup).
   Plain JavaScript, no libraries except the vendored ECharts on the Logs page. */
(function () {
  'use strict';
  var page = (document.getElementById('lg-kpis') || {}).dataset ? document.getElementById('lg-kpis').dataset.page : '';
  var SEV = ['emerg', 'alert', 'crit', 'err', 'warning', 'notice', 'info', 'debug'];

  // -- helpers ---------------------------------------------------------------------------
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fmtTime(epoch) {
    if (!epoch) return '—';
    var d = new Date(epoch * 1000), p = function (n) { return String(n).padStart(2, '0'); };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }
  function ago(epoch) {
    if (!epoch) return 'never';
    var s = Math.max(0, Date.now() / 1000 - epoch);
    if (s < 60) return Math.round(s) + ' s ago';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return (s / 3600).toFixed(1).replace('.0', '') + ' h ago';
    return (s / 86400).toFixed(1).replace('.0', '') + ' days ago';
  }
  function bytes(n) {
    if (n == null) return '—';
    var u = ['B', 'KB', 'MB', 'GB', 'TB'], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)) + ' ' + u[i];
  }
  function bytesHtml(n) {     // "37.0<small>MB</small>" so a status card's number does not wrap
    var t = bytes(n), i = t.lastIndexOf(' ');
    return i < 0 ? t : esc(t.slice(0, i)) + '<small>' + esc(t.slice(i + 1)) + '</small>';
  }
  function num(n) { return n == null ? '—' : Number(n).toLocaleString(); }
  function token() { var t = $('lg-token'); return t ? t.value.trim() : ''; }
  (function keepToken() {
    var t = $('lg-token'); if (!t) return;
    try { t.value = sessionStorage.getItem('vco-token') || ''; } catch (e) {}
    t.addEventListener('change', function () { try { sessionStorage.setItem('vco-token', t.value.trim()); } catch (e) {} });
  })();
  function api(method, url, body) {
    var h = { 'Accept': 'application/json' };
    if (body !== undefined) h['Content-Type'] = 'application/json';
    if (token()) h['X-API-Key'] = token();
    return fetch(url, { method: method, headers: h, body: body === undefined ? undefined : JSON.stringify(body) })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
          return j;
        });
      });
  }
  function sevPill(n, name) {
    return n == null ? '<span class="sev">—</span>' : '<span class="sev sev-' + n + '">' + esc(name || SEV[n]) + '</span>';
  }
  function kpi(title, value, label, level, gauge) {
    var ring = gauge == null ? '' :
      '<svg class="gauge ' + (level || '') + '" viewBox="0 0 36 36" width="56" height="56" aria-hidden="true"><circle class="gauge-track" cx="18" cy="18" r="15.9"/>' +
      '<circle class="gauge-fill" cx="18" cy="18" r="15.9" stroke-dasharray="' + Math.max(0, Math.min(100, gauge)).toFixed(1) + ' 100"/></svg>';
    return '<section class="card kpi-card ' + (level || 'none') + '"><div class="card-header">' + esc(title) + '</div><div class="card-block"><div class="kpi-row">' +
      ring + '<div><div class="kpi-value ' + (level || '') + '">' + value + '</div><div class="subtle">' + label + '</div></div></div></div></section>';
  }
  function formData(form) {
    var out = {};
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name) return;
      out[el.name] = el.type === 'checkbox' ? el.checked : el.value;
    });
    return out;
  }
  function fill(form, data) {
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name || !(el.name in data)) return;
      if (el.type === 'checkbox') el.checked = !!data[el.name];
      else if (el.type !== 'password') el.value = data[el.name] == null ? '' : data[el.name];
    });
  }
  function say(el, text, bad) { if (!el) return; el.textContent = text; el.style.color = bad ? 'var(--crit)' : 'var(--ok)'; }

  // -- status cards (every page) ---------------------------------------------------------
  var lastSummary = null;
  function loadSummary() {
    return api('GET', '/ui/logs/summary').then(function (s) {
      lastSummary = s;
      var k = $('lg-kpis'), html = '';
      var stor = s.size_pct == null ? null : s.size_pct;
      var storLvl = stor == null ? '' : stor >= 95 ? 'crit' : stor >= 80 ? 'warn' : 'ok';
      var diskPct = s.disk_total ? (s.disk_total - s.disk_free) / s.disk_total * 100 : null;
      var listen = s.listener || {};
      var lsub = $('lg-sub');
      if (lsub) lsub.textContent = 'Syslog sent to this server on port ' + (listen.host_port || 514) + ' (UDP and TCP). Kept ' + (s.retention_days || 5) + ' days, up to ' + bytes(s.max_size_bytes) + '.';
      if (page === 'logs') {
        html += kpi('Last hour', num(s.last_hour), 'messages · ' + num(s.per_minute) + ' in the last minute');
        html += kpi('Errors, last hour', num(s.errors_last_hour), 'err or worse · ' + num(s.warnings_last_hour) + ' warnings', s.errors_last_hour ? 'crit' : 'ok');
        html += kpi('Sources', num(s.senders_active), 'sending of ' + num(s.senders_total) + ' seen (hosts and apps)', s.senders_total ? 'ok' : '');
        html += kpi('Unacknowledged alerts', '<a href="/dash/logs/alerts#fired">' + num(s.unacked_alerts) + '</a>', 'on the Log alerts page', s.unacked_alerts ? 'crit' : 'ok');
        html += kpi('Storage', bytesHtml(s.used_bytes), 'of ' + bytes(s.max_size_bytes) + ' cap', storLvl, stor);
      } else if (page === 'alerts') {
        return;   // the alerts page draws its own cards after loading the rules
      } else {
        html += kpi('Listener', listen.running ? 'Running' : 'Stopped', listen.running ? 'UDP ' + (listen.udp_port || 5514) + ' · TCP ' + (listen.tcp_port || 5514) + ' in the container' : esc(listen.last_output || 'see the Modules page'), listen.running ? 'ok' : 'crit');
        html += kpi('Storage', bytesHtml(s.used_bytes), 'of ' + bytes(s.max_size_bytes) + ' cap (' + (stor == null ? '—' : stor + '%') + ')', storLvl, stor);
        html += kpi('Disk free', bytesHtml(s.disk_free), 'of ' + bytes(s.disk_total) + ' on the data volume', diskPct == null ? '' : diskPct >= 95 ? 'crit' : diskPct >= 85 ? 'warn' : 'ok', diskPct == null ? null : 100 - diskPct);
        html += kpi('Rate', num(s.per_second) + '<small>/s</small>', num(s.per_minute) + ' per minute · ' + num(s.per_hour) + ' per hour');
        html += kpi('Oldest message', s.oldest ? ago(s.oldest).replace(' ago', '') : '—', s.oldest ? fmtTime(s.oldest) : 'nothing kept yet');
      }
      k.innerHTML = html;
      return s;
    }).catch(function (e) { $('lg-kpis').innerHTML = '<div class="alert alert-danger">' + esc(e.message) + '</div>'; });
  }

  // =====================================================================================
  // Logs page
  // =====================================================================================
  function logsPage() {
    var form = $('lg-form'), q = $('lg-q'), range = $('lg-range'), sev = $('lg-sev'), host = $('lg-host'), appSel = $('lg-app');
    var rows = $('lg-rows'), more = $('lg-more'), errBox = $('lg-err'), chartEl = $('lg-chart');
    var zoom = null, oldest = null, live = false, liveTimer = null, lastId = 0, shown = 0, terms = [];
    var chart = window.echarts ? echarts.init(chartEl, null, { renderer: 'svg' }) : null;

    // Search state lives in the URL, so a search can be bookmarked or sent to someone.
    var p = new URLSearchParams(location.search);
    q.value = p.get('q') || '';
    if (p.get('range')) range.value = p.get('range');
    if (p.get('sev')) sev.value = p.get('sev');
    if (p.get('since')) zoom = { since: +p.get('since'), until: p.get('until') ? +p.get('until') : null };

    function params(extra) {
      var o = { q: q.value.trim(), sev: sev.value, host: host.value, app: appSel.value };
      if (zoom) { o.since = zoom.since; if (zoom.until) o.until = zoom.until; o.range = ''; }
      else o.range = range.value === 'all' ? '' : range.value;
      Object.keys(extra || {}).forEach(function (k) { o[k] = extra[k]; });
      var u = new URLSearchParams();
      Object.keys(o).forEach(function (k) { if (o[k] !== '' && o[k] != null) u.set(k, o[k]); });
      return u;
    }
    function highlight(text) {
      var out = esc(text);
      terms.forEach(function (t) {
        if (t.length < 2) return;
        var re = new RegExp('(' + esc(t).replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
        out = out.replace(re, '<mark class="lg-mark">$1</mark>');
      });
      return out;
    }
    // Long messages (an app's prompt or the model's thinking) are cut in the list; the row's
    // details show all of it.
    var CUT = 500;
    function msgCell(text) {
      text = text == null ? '' : String(text);
      if (text.length <= CUT) return highlight(text);
      return highlight(text.slice(0, CUT)) + '… <span class="lg-meta">(' + num(text.length - CUT) + ' more characters, click to read all)</span>';
    }
    function rowHtml(r, isNew) {
      return '<tr class="row' + (isNew ? ' new' : '') + '" data-id="' + r.id + '" tabindex="0">' +
        '<td class="t">' + fmtTime(r.ts) + '</td><td class="h">' + esc(r.host) + '</td><td>' + sevPill(r.severity, r.severity_name) + '</td>' +
        '<td>' + esc(r.app || '') + (r.msgid ? '<div class="lg-meta">' + esc(r.msgid) + '</div>' : '') + '</td><td class="m">' + msgCell(r.message) + '</td></tr>';
    }
    var byId = {};
    function detailHtml(r) {
      var f = [['Received', fmtTime(r.ts) + ' (' + r.time + ')'], ['Time in message', r.msg_time], ['Source', r.host], ['Sender IP', r.ip],
        ['Severity', r.severity_name ? r.severity_name + ' (' + r.severity + ')' : null], ['Facility', r.facility_name ? r.facility_name + ' (' + r.facility + ')' : null],
        ['App', r.app], ['Process ID', r.procid], ['Message ID', r.msgid], ['Protocol', r.proto]];
      var html = '<dl class="lg-detail">';
      f.forEach(function (x) { if (x[1] != null && x[1] !== '') html += '<dt>' + x[0] + '</dt><dd>' + esc(x[1]) + '</dd>'; });
      if (r.sd) html += '<dt>Structured data</dt><dd><pre>' + esc(JSON.stringify(r.sd, null, 2)) + '</pre></dd>';
      html += '<dt>Message</dt><dd><pre>' + esc(r.message) + '</pre></dd></dl><div class="lg-actions">' +
        '<button type="button" class="btn btn-outline" data-add="host:' + esc(r.host) + '">Only this source</button>' +
        '<button type="button" class="btn btn-outline" data-add="-host:' + esc(r.host) + '">Hide this source</button>' +
        (r.app ? '<button type="button" class="btn btn-outline" data-add="app:' + esc(r.app) + '">Only this app</button>' : '') +
        '<button type="button" class="btn btn-outline" data-around="' + r.ts + '">Messages around this one</button></div>';
      return html;
    }
    rows.addEventListener('click', function (e) {
      var add = e.target.closest('[data-add]');
      if (add) { q.value = (q.value.trim() + ' ' + add.dataset.add).trim(); run(); return; }
      var around = e.target.closest('[data-around]');
      if (around) {
        var t = +around.dataset.around; q.value = ''; sev.value = '';
        zoom = { since: t - 300, until: t + 300 }; run(); return;
      }
      var tr = e.target.closest('tr.row');
      if (!tr || window.getSelection().toString()) return;
      var next = tr.nextElementSibling;
      if (next && next.classList.contains('detail')) { next.remove(); return; }
      var r = byId[tr.dataset.id]; if (!r) return;
      tr.insertAdjacentHTML('afterend', '<tr class="detail"><td colspan="5">' + detailHtml(r) + '</td></tr>');
    });
    rows.addEventListener('keydown', function (e) { if (e.key === 'Enter' && e.target.matches('tr.row')) e.target.click(); });

    function showError(msg) { errBox.hidden = !msg; errBox.textContent = msg || ''; }
    function updateUrl() {
      var u = params();
      history.replaceState(null, '', location.pathname + (u.toString() ? '?' + u : ''));
      $('lg-csv').href = '/ui/logs/export.csv?' + params().toString();
      $('lg-unzoom').hidden = !zoom;
      range.value = zoom ? 'custom' : (range.value === 'custom' ? '1h' : range.value);
    }

    function run() {
      oldest = null; byId = {};
      terms = (q.value.match(/"[^"]+"|\S+/g) || []).filter(function (t) { return t[0] !== '-' && !/^\w+:/.test(t); })
        .map(function (t) { return t.replace(/^"|"$/g, '').replace(/\*$/, ''); });
      updateUrl();
      showError('');
      loadPage(false);
      loadChart();
    }
    function loadPage(append) {
      var u = params({ limit: 200 });
      if (append && oldest) u.set('before', oldest);
      more.disabled = true;
      return api('GET', '/ui/logs/search?' + u).then(function (res) {
        var html = res.items.map(function (r) { byId[r.id] = r; return rowHtml(r); }).join('');
        if (append) rows.insertAdjacentHTML('beforeend', html); else rows.innerHTML = html;
        if (res.items.length) oldest = res.items[res.items.length - 1].id;
        if (!append) {
          shown = res.items.length;
          lastId = res.items.length ? res.items[0].id : lastId;
          $('lg-count').textContent = (res.total_capped ? 'More than ' : '') + num(res.total) + ' message' + (res.total === 1 ? '' : 's');
          $('lg-took').textContent = 'searched in ' + res.took_ms + ' ms';
        } else shown += res.items.length;
        $('lg-none').hidden = shown > 0;
        more.hidden = !res.more; more.disabled = false;
      }).catch(function (e) { showError(e.message); rows.innerHTML = ''; $('lg-count').textContent = 'Messages'; more.hidden = true; });
    }
    function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
    function loadChart() {
      if (!chart) return;
      api('GET', '/ui/logs/histogram?' + params({ buckets: 60 })).then(function (h) {
        var cats = h.buckets.map(function (b) { return b.t; });
        var spanDays = (h.end - h.start) / 86400;
        var label = function (t) {
          var d = new Date(t * 1000), p = function (n) { return String(n).padStart(2, '0'); };
          return (spanDays > 1 ? (d.getMonth() + 1) + '/' + d.getDate() + ' ' : '') + p(d.getHours()) + ':' + p(d.getMinutes());
        };
        var text = cssVar('--subtle'), grid = cssVar('--border');
        var anim = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        chart.setOption({
          animation: anim, grid: { left: 44, right: 12, top: 26, bottom: 22 },
          legend: { top: 0, right: 0, textStyle: { color: text }, itemWidth: 12, itemHeight: 8 },
          tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: function (v) { return num(v); },
            formatter: undefined },
          xAxis: { type: 'category', data: cats.map(label), axisLabel: { color: text, hideOverlap: true }, axisLine: { lineStyle: { color: grid } }, axisTick: { show: false } },
          yAxis: { type: 'value', minInterval: 1, axisLabel: { color: text }, splitLine: { lineStyle: { color: grid } } },
          series: [
            { name: 'Error or worse', type: 'bar', stack: 'a', data: h.buckets.map(function (b) { return b.error; }), itemStyle: { color: cssVar('--crit') } },
            { name: 'Warning', type: 'bar', stack: 'a', data: h.buckets.map(function (b) { return b.warning; }), itemStyle: { color: cssVar('--warn-fill') } },
            { name: 'Other', type: 'bar', stack: 'a', data: h.buckets.map(function (b) { return b.other; }), itemStyle: { color: cssVar('--primary') }, barCategoryGap: '15%' }
          ]
        }, true);
        chart.off('click');
        chart.on('click', function (ev) {
          var t = h.buckets[ev.dataIndex].t;
          zoom = { since: t, until: t + h.step };
          run();
        });
      }).catch(function () { chart.clear(); });
    }
    window.addEventListener('resize', function () { if (chart) chart.resize(); });
    new MutationObserver(function () { loadChart(); }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

    form.addEventListener('submit', function (e) { e.preventDefault(); zoom = null; run(); });
    $('lg-chips').addEventListener('click', function (e) {
      var b = e.target.closest('.lg-chip'); if (!b) return;
      if ('q' in b.dataset) q.value = b.dataset.q;
      if ('app' in b.dataset) { ensureOption(appSel, b.dataset.app); appSel.value = b.dataset.app; }
      if ('host' in b.dataset) host.value = b.dataset.host;
      zoom = null; run();
    });
    [range, sev, host, appSel].forEach(function (el) { el.addEventListener('change', function () { if (el === range) zoom = null; run(); }); });
    $('lg-unzoom').addEventListener('click', function () { zoom = null; if (range.value === 'custom') range.value = '1h'; run(); });
    more.addEventListener('click', function () { loadPage(true); });

    // Live tail: new messages that match the search appear at the top every 2 seconds.
    var liveBtn = $('lg-live');
    function tick() {
      var u = params({ after: lastId }); u.delete('range'); u.delete('since'); u.delete('until');
      api('GET', '/ui/logs/tail?' + u).then(function (res) {
        if (res.items.length) {
          Array.prototype.forEach.call(rows.querySelectorAll('tr.new'), function (tr) { tr.classList.remove('new'); });
          var html = res.items.slice().reverse().map(function (r) { byId[r.id] = r; return rowHtml(r, true); }).join('');
          rows.insertAdjacentHTML('afterbegin', html);
          shown += res.items.length;
          $('lg-none').hidden = true;
          while (rows.children.length > 1500) rows.lastElementChild.remove();
        }
        lastId = res.last || lastId;
      }).catch(function () {}).then(function () { if (live) liveTimer = setTimeout(tick, 2000); });
    }
    liveBtn.addEventListener('click', function () {
      live = !live;
      liveBtn.classList.toggle('on', live);
      liveBtn.setAttribute('aria-pressed', live ? 'true' : 'false');
      liveBtn.lastElementChild.textContent = live ? 'Live: on' : 'Live tail';
      clearTimeout(liveTimer);
      if (live) {
        if (zoom) { zoom = null; range.value = '1h'; run(); }
        api('GET', '/ui/logs/tail?after=0').then(function (r) { lastId = Math.max(lastId, r.last || 0); tick(); });
      }
    });

    // Sources (anything that sends: hosts, appliances, apps) and app names for the two menus.
    function ensureOption(sel, value) {
      if (!value || Array.prototype.some.call(sel.options, function (o) { return o.value === value; })) return;
      var o = document.createElement('option'); o.value = value; o.textContent = value; sel.appendChild(o);
    }
    Promise.all([
      api('GET', '/ui/logs/hosts').then(function (h) {
        h.items.slice().sort(function (a, b) { return (a.host || '').localeCompare(b.host || ''); }).forEach(function (x) {
          if (!x.host) return;
          var o = document.createElement('option'); o.value = x.host; o.textContent = x.host + (x.quiet ? ' (idle)' : '');
          host.appendChild(o);
        });
      }).catch(function () {}),
      api('GET', '/ui/logs/apps').then(function (a) {
        a.items.forEach(function (x) { ensureOption(appSel, x.app); });
      }).catch(function () {})
    ]).then(function () {
      if (p.get('host')) { ensureOption(host, p.get('host')); host.value = p.get('host'); }
      if (p.get('app')) { ensureOption(appSel, p.get('app')); appSel.value = p.get('app'); }
      run();
    });
    setInterval(loadSummary, 15000);
  }

  // =====================================================================================
  // Log alerts page
  // =====================================================================================
  function alertsPage() {
    var editor = $('al-editor'), aform = $('al-form'), msg = $('al-msg'), data = null;
    var nform = $('nt-form'), nmsg = $('nt-msg');

    function cards(d) {
      var on = d.rules.filter(function (r) { return r.enabled; }).length;
      $('al-off').hidden = !!d.alerts_enabled;
      $('lg-kpis').innerHTML =
        kpi('Alert rules', num(on), 'on, of ' + num(d.rules.length), d.alerts_enabled ? (on ? 'ok' : '') : 'warn') +
        kpi('Unacknowledged', '<a href="#fired">' + num(d.unacked) + '</a>', 'fired alerts to look at', d.unacked ? 'crit' : 'ok') +
        kpi('Checking', d.alerts_enabled ? 'On' : 'Off', d.alerts_enabled ? 'every 30 seconds' : 'turned off under Notifications', d.alerts_enabled ? 'ok' : 'warn') +
        kpi('Email', d.email_ready ? 'Ready' : 'Not set', d.email_ready ? 'SMTP server and To set' : 'set it under Notifications', d.email_ready ? 'ok' : '') +
        kpi('Webhook', d.webhook_ready ? 'Ready' : 'Not set', d.webhook_ready ? 'URL set' : 'set it under Notifications', d.webhook_ready ? 'ok' : '');
    }
    function cond(r) {
      return '≥ ' + num(r.threshold) + ' in ' + r.window_minutes + ' min<div class="lg-meta">then wait ' + r.cooldown_minutes + ' min</div>';
    }
    function load() {
      return api('GET', '/ui/logs/alerts').then(function (d) {
        data = d; cards(d);
        $('al-rules').innerHTML = d.rules.map(function (r) {
          return '<tr><td><label class="switch" style="font-weight:400" title="On / off"><input type="checkbox" role="switch" data-toggle="' + r.id + '" ' + (r.enabled ? 'checked' : '') + ' aria-label="' + esc(r.name) + ' on"><span></span></label></td>' +
            '<td><b>' + esc(r.name) + '</b></td><td class="msg"><code>' + esc(r.query || '(every message)') + '</code></td>' +
            '<td>' + (r.min_severity_name ? sevPill(r.min_severity, r.min_severity_name) + (r.min_severity > 0 ? ' or worse' : '') : 'any') + '</td>' +
            '<td>' + cond(r) + '</td><td>' + (['page'].concat(r.notify_email ? ['email'] : [], r.notify_webhook ? ['webhook'] : [])).join(', ') + '</td>' +
            '<td>' + (r.last_fired ? ago(r.last_fired) + '<div class="lg-meta">' + num(r.last_count) + ' messages</div>' : 'never') + '</td>' +
            '<td style="white-space:nowrap"><a class="btn btn-outline" href="/dash/logs/search?' + new URLSearchParams({ q: r.query || '', sev: r.min_severity_name || '', range: '24h' }) + '">Logs</a> ' +
            '<button type="button" class="btn btn-outline" data-edit="' + r.id + '">Edit</button> ' +
            '<button type="button" class="btn btn-outline" data-del="' + r.id + '">Delete</button></td></tr>';
        }).join('');
        $('al-rules-none').hidden = d.rules.length > 0;
        $('al-events').innerHTML = d.events.map(function (e) {
          var since = Math.floor(e.fired_at - e.window_minutes * 60);
          return '<tr><td><input type="checkbox" data-ev="' + e.id + '" aria-label="Select"></td><td>' + fmtTime(e.fired_at) + '<div class="lg-meta">' + ago(e.fired_at) + '</div></td>' +
            '<td><b>' + esc(e.rule_name) + '</b><div class="lg-meta"><code>' + esc(e.query || '(every message)') + '</code></div>' +
            (e.sample && e.sample.length ? '<details><summary class="lg-meta">Newest ' + e.sample.length + '</summary><ul class="lg-sample">' + e.sample.map(function (s) {
              return '<li>' + esc(s.host) + ' ' + sevPill(s.severity, s.severity_name) + ' ' + esc((s.message || '').slice(0, 300)) + '</li>'; }).join('') + '</ul></details>' : '') + '</td>' +
            '<td class="num">' + num(e.count) + '<div class="lg-meta">in ' + e.window_minutes + ' min</div></td>' +
            '<td class="msg">' + esc(e.notified || (e.acked ? '' : 'sending…')) + '</td>' +
            '<td>' + (e.acked ? '<span class="label">acknowledged</span>' : '<span class="label label-crit">new</span>') + '</td>' +
            '<td><a class="btn btn-outline" href="/dash/logs/search?' + new URLSearchParams({ q: e.query || '', since: since, until: Math.ceil(e.fired_at) + 1 }) + '">Show messages</a></td></tr>';
        }).join('');
        $('al-events-none').hidden = d.events.length > 0;
        $('ev-all').checked = false; selection();
      }).catch(function (e) { $('lg-kpis').innerHTML = '<div class="alert alert-danger">' + esc(e.message) + '</div>'; });
    }
    function selection() {
      var n = document.querySelectorAll('#al-events input[data-ev]:checked').length;
      $('ev-ack').disabled = !n; $('ev-del').disabled = !n;
      $('ev-ack').textContent = n ? 'Acknowledge selected (' + n + ')' : 'Acknowledge selected';
      $('ev-del').textContent = n ? 'Delete selected (' + n + ')' : 'Delete selected';
    }
    function openEditor(rule) {
      aform.reset();
      fill(aform, rule || { enabled: true, threshold: 1, window_minutes: 5, cooldown_minutes: 30, min_severity: '' });
      if (rule) aform.elements.id.value = rule.id; else aform.elements.id.value = '';
      if (rule) aform.elements.min_severity.value = rule.min_severity == null ? '' : String(rule.min_severity);
      editor.hidden = false; $('al-preview-out').innerHTML = ''; msg.textContent = '';
      $('rules').classList.add('lg-editing');
      $('al-name').focus();
    }
    function closeEditor() { editor.hidden = true; $('rules').classList.remove('lg-editing'); }
    function ruleBody() {
      var b = formData(aform);
      b.min_severity = b.min_severity === '' ? null : +b.min_severity;
      if (!b.id) delete b.id;
      return b;
    }
    $('al-new').addEventListener('click', function () { openEditor(null); });
    $('al-cancel').addEventListener('click', closeEditor);
    $('al-save').addEventListener('click', function () {
      if (!aform.reportValidity()) return;
      api('POST', '/ui/logs/alerts', ruleBody()).then(function () { closeEditor(); load(); })
        .catch(function (e) { say(msg, e.message, true); });
    });
    $('al-preview').addEventListener('click', function () {
      var b = ruleBody(); b.name = b.name || 'preview';
      api('POST', '/ui/logs/alerts/preview', b).then(function (r) {
        $('al-preview-out').innerHTML = '<p class="' + (r.would_fire ? 'lg-error' : 'lg-ok') + '">' + num(r.count) + ' message' + (r.count === 1 ? '' : 's') +
          ' match in the last ' + esc(b.window_minutes) + ' minutes, so this alert ' + (r.would_fire ? '<b>would fire now</b>.' : 'would not fire now.') + '</p>' +
          (r.sample.length ? '<ul class="lg-sample">' + r.sample.map(function (s) { return '<li>' + fmtTime(s.ts) + ' ' + esc(s.host) + ' ' + sevPill(s.severity, s.severity_name) + ' ' + esc((s.message || '').slice(0, 240)) + '</li>'; }).join('') + '</ul>' : '');
      }).catch(function (e) { say(msg, e.message, true); });
    });
    $('al-rules').addEventListener('click', function (e) {
      var ed = e.target.closest('[data-edit]'), del = e.target.closest('[data-del]');
      if (ed) openEditor(data.rules.filter(function (r) { return String(r.id) === ed.dataset.edit; })[0]);
      if (del) {
        var r = data.rules.filter(function (x) { return String(x.id) === del.dataset.del; })[0];
        vcoConfirm('Delete the alert "' + r.name + '"? Alerts it already fired stay in the list.', { ok: 'Delete', danger: true }).then(function (yes) {
          if (yes) api('POST', '/ui/logs/alerts/' + r.id + '/delete').then(load).catch(function (err) { vcoAlert(err.message); });
        });
      }
    });
    $('al-rules').addEventListener('change', function (e) {
      var t = e.target.closest('[data-toggle]'); if (!t) return;
      api('POST', '/ui/logs/alerts/' + t.dataset.toggle + '/enabled', { enabled: t.checked }).then(load)
        .catch(function (err) { t.checked = !t.checked; vcoAlert(err.message); });
    });
    $('al-events').addEventListener('change', selection);
    $('ev-all').addEventListener('change', function () {
      Array.prototype.forEach.call(document.querySelectorAll('#al-events input[data-ev]'), function (c) { c.checked = $('ev-all').checked; });
      selection();
    });
    function selected() { return Array.prototype.map.call(document.querySelectorAll('#al-events input[data-ev]:checked'), function (c) { return +c.dataset.ev; }); }
    $('ev-ack').addEventListener('click', function () { api('POST', '/ui/logs/events/ack', { ids: selected() }).then(load).catch(function (e) { vcoAlert(e.message); }); });
    $('ev-ack-all').addEventListener('click', function () { api('POST', '/ui/logs/events/ack', { all: true }).then(load).catch(function (e) { vcoAlert(e.message); }); });
    $('ev-del').addEventListener('click', function () {
      var ids = selected();
      vcoConfirm('Delete ' + ids.length + ' fired alert' + (ids.length === 1 ? '' : 's') + ' from the list?', { ok: 'Delete', danger: true }).then(function (yes) {
        if (yes) api('POST', '/ui/logs/events/ack', { ids: ids, delete: true }).then(load).catch(function (e) { vcoAlert(e.message); });
      });
    });

    // Notifications
    function enabledText() { $('nt-enabled-text').textContent = $('nt-enabled').checked ? 'On' : 'Off'; }
    $('nt-enabled').addEventListener('change', enabledText);
    $('al-turn-on').addEventListener('click', function () {
      api('PUT', '/ui/logs/settings', { alerts_enabled: true }).then(function () { loadNotify(); load(); })
        .catch(function (e) { if (window.vcoAlert) vcoAlert(e.message); else alert(e.message); });
    });
    function loadNotify() {
      return api('GET', '/ui/logs/settings').then(function (s) {
        fill(nform, s);
        enabledText();
        nform.elements.smtp_password.placeholder = s.smtp_password_set ? 'saved (leave blank to keep)' : 'optional';
      });
    }
    $('nt-save').addEventListener('click', function () {
      var b = formData(nform);
      var go = b.alerts_enabled ? Promise.resolve(true) : (window.vcoConfirm ?
        vcoConfirm('Check alert rules is off: no rule will fire and no email or webhook will be sent. Save it off?', { ok: 'Save with checking off', danger: true }) :
        Promise.resolve(confirm('Check alert rules is off. Save it off?')));
      go.then(function (yes) { if (!yes) return;
      api('PUT', '/ui/logs/settings', b).then(function () { say(nmsg, 'Saved.'); nform.elements.smtp_password.value = ''; loadNotify(); load(); })
        .catch(function (e) { say(nmsg, e.message, true); });
      });
    });
    ['email', 'hook'].forEach(function (k) {
      $('nt-test-' + k).addEventListener('click', function () {
        say(nmsg, 'Sending…');
        api('POST', '/ui/logs/notify/test', { kind: k === 'hook' ? 'webhook' : 'email' })
          .then(function (r) { say(nmsg, r.result || 'Sent.', !r.ok); })
          .catch(function (e) { say(nmsg, e.message, true); });
      });
    });
    load(); loadNotify();
    setInterval(load, 30000);
  }

  // =====================================================================================
  // Log settings page
  // =====================================================================================
  function settingsPage() {
    var form = $('st-form'), msg = $('st-msg');
    // The address senders should use. Opened as localhost, the page can't know the
    // computer's network address, so it shows a placeholder instead.
    var addr = /^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname) ? '<this-computer-ip>' : location.hostname;
    Array.prototype.forEach.call(document.querySelectorAll('.lg-addr'), function (el) { el.textContent = addr; });
    function ports(s) {   // the host port docker-compose maps (SYSLOG_PORT in .env), in every example
      var hp = (s.listener || {}).host_port || 514;
      Array.prototype.forEach.call(document.querySelectorAll('.lg-port'), function (el) { el.textContent = hp; });
      var note = $('st-port-note');
      if (note && hp !== 514) { note.hidden = false; note.textContent = 'This server uses port ' + hp + ' (SYSLOG_PORT in .env) instead of the standard 514, so every example below uses ' + hp + '.'; }
    }
    function listener(s) {
      ports(s);
      var l = s.listener || {};
      var errs = (l.errors || []).slice(-5);
      $('st-listener').innerHTML = '<dl class="lg-detail">' +
        '<dt>Status</dt><dd>' + (l.running ? '<span class="label label-ok">running</span> since ' + esc(l.since || '') + ' UTC' : '<span class="label label-crit">not running</span> ' + esc(l.last_output || '')) + '</dd>' +
        '<dt>Ports</dt><dd>UDP ' + (l.udp_port || 5514) + ' and TCP ' + (l.tcp_port || 5514) + ' in the container; docker-compose.yml maps the host\'s port ' + (l.host_port || 514) + ' to them (SYSLOG_PORT in .env)</dd>' +
        '<dt>Received</dt><dd>' + num(l.received) + ' since the listener started' + (l.dropped ? ' · <span class="text-danger">' + num(l.dropped) + ' dropped (too fast to write)</span>' : '') + '</dd>' +
        '<dt>Rate</dt><dd>' + num(s.per_second) + ' per second · ' + num(s.per_minute) + ' per minute · ' + num(s.per_hour) + ' per hour</dd>' +
        '<dt>TCP connections</dt><dd>' + num(l.tcp_clients || 0) + '</dd>' +
        '<dt>Database</dt><dd>' + bytes(s.used_bytes) + ' used, file ' + bytes(s.file_bytes) + ' · disk free ' + bytes(s.disk_free) + '</dd>' +
        (errs.length ? '<dt>Recent problems</dt><dd>' + errs.map(esc).join('<br>') + '</dd>' : '') +
        '</dl><p class="lg-meta">Restarts and the full output are on the <a href="/modules#logs">Modules</a> page.</p>';
    }
    function senders() {
      return api('GET', '/ui/logs/hosts').then(function (h) {
        $('st-senders').innerHTML = h.items.map(function (x) {
          var apps = (x.apps || []).map(function (a) {
            return '<a href="/dash/logs/search?' + new URLSearchParams({ host: x.host, app: a, range: '24h' }) + '">' + esc(a) + '</a>';
          }).join(', ');
          return '<tr><td><a href="/dash/logs/search?' + new URLSearchParams({ host: x.host, range: '24h' }) + '">' + esc(x.host) + '</a></td><td>' + esc(x.ip || '') + '</td>' +
            '<td>' + (apps || '<span class="lg-meta">—</span>') + '</td>' +
            '<td>' + ago(x.last_seen) + '<div class="lg-meta">' + fmtTime(x.last_seen) + '</div></td><td class="num">' + num(x.messages_24h) + '</td>' +
            '<td class="num">' + (x.errors_24h ? '<span class="label label-crit">' + num(x.errors_24h) + '</span>' : '0') + '</td>' +
            '<td>' + (x.quiet ? '<span class="label lg-idle">idle</span>' : '<span class="label label-ok">sending</span>') + '</td></tr>';
        }).join('');
        $('st-senders-none').hidden = h.items.length > 0;
        $('st-senders-meta').textContent = h.items.length + ' source' + (h.items.length === 1 ? '' : 's') + ' · idle after ' + h.quiet_minutes + ' min without a message';
      });
    }
    api('GET', '/ui/logs/settings').then(function (s) { fill(form, s); });
    $('st-save').addEventListener('click', function () {
      if (!form.reportValidity()) return;
      api('PUT', '/ui/logs/settings', formData(form)).then(function (r) { say(msg, 'Saved. ' + r.note); loadSummary().then(function (s) { if (s) listener(s); }); senders(); })
        .catch(function (e) { say(msg, e.message, true); });
    });
    function refresh() { loadSummary().then(function (s) { if (s) listener(s); }); senders(); }
    refresh();
    setInterval(refresh, 15000);
  }

  if (page === 'logs') loadSummary();
  if (page === 'logs') logsPage();
  else if (page === 'alerts') alertsPage();
  else if (page === 'settings') settingsPage();
})();
