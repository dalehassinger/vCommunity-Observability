/* vCommunity Ask Performance page: polls /ui/perf every 10 seconds and draws KPI cards,
   ECharts time series and the Ollama and host tables. Everything is built from numbers
   (textContent only), so nothing from the agent or Ollama is ever run as HTML. */
(function () {
  'use strict';
  var hours = 1, data = null, charts = {};
  try { hours = Number(localStorage.getItem('ask.perf.hours')) || 1; } catch (e) { /* private window */ }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function css(name, fb) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fb; }
  function bytes(n, digits) {
    if (n == null) return '–';
    var u = ['B', 'KB', 'MB', 'GB', 'TB'], i = 0;
    while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? (digits == null ? 1 : digits) : 0) + ' ' + u[i];
  }
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return '&#' + c.charCodeAt(0) + ';'; }); }
  function pct(n) { return n == null ? '–' : Math.round(n) + '%'; }
  function level(v, warn, crit) { return v == null ? '' : v >= crit ? 'crit' : v >= warn ? 'warn' : 'ok'; }
  function ago(ts) {
    var s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    return s < 60 ? s + ' s ago' : s < 3600 ? Math.round(s / 60) + ' min ago' : Math.round(s / 3600) + ' h ago';
  }
  function avg(a) { a = a.filter(function (x) { return x != null; }); return a.length ? a.reduce(function (t, x) { return t + x; }, 0) / a.length : null; }

  // -- KPI cards (VCF Operations style: colored edge by state) ----------------------
  function kpi(label, value, sub, state) {
    var c = el('div', 'perf-kpi ' + (state || ''));
    c.appendChild(el('div', 'perf-kpi-label', label));
    c.appendChild(el('div', 'perf-kpi-value', value));
    c.appendChild(el('div', 'perf-kpi-sub', sub || ''));
    return c;
  }
  function renderKpis() {
    var box = document.getElementById('perf-kpis');
    box.innerHTML = '';
    var n = data.now, c = n.container || {}, o = n.ollama || {}, h = n.host && n.host.ok ? n.host : null;
    var recent = data.answers.slice(-20);
    if (h) {
      var cores = (h.host && h.host.cores) || {};
      box.appendChild(kpi('Host CPU', pct(h.cpu && h.cpu.pct), (cores.p ? cores.p + 'P + ' + cores.e + 'E cores' : (cores.total || '?') + ' cores') +
        (h.load ? ' · load ' + h.load[0] : ''), level(h.cpu && h.cpu.pct, 70, 90)));
      box.appendChild(kpi('Host GPU', h.gpu ? pct(h.gpu.pct) : 'n/a', h.gpu && h.gpu.mem_used != null ? bytes(h.gpu.mem_used) + ' GPU memory in use' : 'Not reported on this host',
        h.gpu ? level(h.gpu.pct, 85, 98) : ''));
      var m = h.memory || {};
      box.appendChild(kpi('Host memory', bytes(m.used), 'of ' + bytes(m.total) + (m.pressure_pct != null ? ' · pressure ' + pct(m.pressure_pct) : ''),
        level(m.pressure_pct != null ? m.pressure_pct : m.used_pct, 70, 85)));
    }
    var gpuShare = o.models && o.models.length ? Math.round(avg(o.models.map(function (x) { return x.gpu_pct; }))) : null;
    box.appendChild(kpi('Ollama', o.ok ? (o.models.length + (o.models.length === 1 ? ' model' : ' models')) : 'Offline',
      o.ok ? (o.models.length ? bytes(o.mem) + ' loaded · ' + gpuShare + '% on GPU' : 'Nothing loaded · v' + (o.version || '?')) : (o.error || ''),
      !o.ok ? 'crit' : gpuShare != null && gpuShare < 100 ? 'warn' : 'ok'));
    var tps = avg(recent.map(function (a) { return a.tok_s; })), secs = avg(recent.map(function (a) { return a.seconds; }));
    box.appendChild(kpi('Answer speed', tps == null ? '–' : tps.toFixed(1) + ' tok/s',
      recent.length ? 'last ' + recent.length + ' answers · ' + (secs == null ? '?' : secs.toFixed(1)) + ' s each' : 'No answers yet', tps == null ? '' : tps < 5 ? 'warn' : 'ok'));
    box.appendChild(kpi('Ask container', pct(c.cpu_pct), 'CPU · ' + bytes(c.mem) + (c.mem_limit ? ' of ' + bytes(c.mem_limit, 0) : '') + ' memory',
      level(Math.max(c.cpu_pct || 0, c.mem_pct || 0), 70, 90)));
    var v = n.vco || {};
    box.appendChild(v.running
      ? kpi('Observability container', pct(v.cpu_pct), 'CPU · ' + bytes(v.mem) + (v.mem_limit ? ' of ' + bytes(v.mem_limit, 0) : '') + ' memory',
          level(Math.max(v.cpu_pct || 0, v.mem_pct || 0), 70, 90))
      : kpi('Observability container', 'Not running', 'The vco container did not answer', 'crit'));
  }

  function renderAgentNote() {
    var note = document.getElementById('perf-agent-note');
    var h = data.now.host;
    note.innerHTML = '';
    if (h && h.ok) { note.hidden = true; return; }
    note.hidden = false;
    note.appendChild(el('b', null, 'Host numbers are off. '));
    if (!data.agent_url) {
      note.appendChild(document.createTextNode('Set the Host agent URL on the Settings page to show the Mac’s CPU, GPU and memory.'));
      return;
    }
    note.appendChild(document.createTextNode((h && h.error) || 'No answer from the host agent.'));
    note.appendChild(el('div', 'subtle', 'Install it on the Mac with pwsh ./ask/agent/Install-AskAgent.ps1 (or ./ask/agent/install.sh), put the token it prints in .env as ASK_AGENT_TOKEN, then docker compose up -d vco-ask.' +
      (data.agent_token_set ? '' : ' ASK_AGENT_TOKEN is not set in this container yet.')));
  }

  // -- charts ------------------------------------------------------------------------
  var PALETTE = ['#0072a3', '#3c8500', '#c27b00', '#8939ad', '#00a1b3', '#db2100'];
  var PALETTE_DARK = ['#49afd9', '#5eb715', '#f5c24b', '#c58ae0', '#2fc7d6', '#ff6b52'];
  function base() {
    var dark = document.documentElement.getAttribute('data-theme') === 'dark';
    var text = css('--text', '#21333b'), subtle = css('--subtle', '#5d6c74'), border = css('--border', '#d7dde0');
    return {
      color: dark ? PALETTE_DARK : PALETTE, backgroundColor: 'transparent', animation: false,
      textStyle: { color: text, fontFamily: getComputedStyle(document.body).fontFamily },
      tooltip: { trigger: 'axis', confine: true },
      legend: { top: 0, textStyle: { color: subtle }, itemWidth: 14, itemHeight: 8 },
      grid: { left: 8, right: 12, top: 28, bottom: 4, containLabel: true },
      xAxis: { type: 'time', axisLabel: { color: subtle, hideOverlap: true }, axisLine: { lineStyle: { color: border } }, splitLine: { show: false } },
      _axis: function (name, fmt, extra) {
        return Object.assign({ type: 'value', name: name, nameTextStyle: { color: subtle, fontSize: 11 },
          axisLabel: { color: subtle, formatter: fmt }, splitLine: { lineStyle: { color: border } } }, extra || {});
      }
    };
  }
  function line(name, pts, axis, area) {
    return { type: 'line', name: name, showSymbol: false, smooth: false, yAxisIndex: axis || 0, data: pts, connectNulls: false,
      lineStyle: { width: 1.6 }, areaStyle: area ? { opacity: 0.12 } : undefined };
  }
  function series(key, scale) {
    return data.history.map(function (s) { var v = s[key]; return [s.ts * 1000, v == null ? null : scale ? v / scale : v]; });
  }
  function draw(name, option, empty) {
    var box = document.querySelector('[data-chart="' + name + '"]');
    if (!box || !window.echarts) return;
    if (empty) { if (charts[name]) { charts[name].dispose(); delete charts[name]; } box.innerHTML = ''; box.appendChild(el('div', 'perf-empty', empty)); return; }
    if (!charts[name]) { box.innerHTML = ''; charts[name] = echarts.init(box, null, { renderer: 'canvas' }); }
    delete option._axis;
    charts[name].setOption(option, true);
  }
  function hasData(keys) { return data.history.some(function (s) { return keys.some(function (k) { return s[k] != null; }); }); }

  function renderCharts() {
    var GB = 1073741824, b;
    // Host CPU / GPU / Ollama
    b = base();
    b.yAxis = b._axis('%', '{value}', { min: 0, max: 100 });
    b.series = [line('CPU', series('h_cpu'), 0, true), line('GPU', series('h_gpu')), line('Ollama CPU', series('h_ollama_cpu'))];
    draw('host-load', b, hasData(['h_cpu', 'h_gpu']) ? null : 'No host agent data yet.');
    // Host memory
    b = base();
    b.yAxis = [b._axis('GB', '{value}', { min: 0 }), b._axis('pressure %', '{value}', { min: 0, max: 100, splitLine: { show: false } })];
    b.series = [line('Used', series('h_mem', GB), 0, true), line('Ollama models', series('o_mem', GB)), line('Pressure', series('h_press'), 1)];
    draw('host-mem', b, hasData(['h_mem', 'o_mem']) ? null : 'No host agent data yet.');
    // Answers
    var ans = data.answers;
    b = base();
    b.xAxis = { type: 'category', data: ans.map(function (a) { return new Date(a.ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }),
      axisLabel: { show: false }, axisLine: { lineStyle: { color: css('--border', '#ccc') } } };
    b.tooltip = { trigger: 'axis', confine: true, formatter: function (p) {
      var a = ans[p[0].dataIndex];
      // Chat titles are user text: escape them; the markers are ECharts' own HTML.
      return [esc(a.title), esc(a.model + (a.kind ? ' · ' + a.kind : ''))].concat(
        p.map(function (x) { return x.marker + esc(x.seriesName) + ': ' + esc(x.value == null ? '–' : x.value); })).join('<br>');
    } };
    b.yAxis = [b._axis('tok/s', '{value}', { min: 0 }), b._axis('seconds', '{value}', { min: 0, splitLine: { show: false } })];
    b.series = [{ type: 'bar', name: 'Tokens/s', data: ans.map(function (a) { return a.tok_s; }), barMaxWidth: 14 },
      { type: 'line', name: 'Answer time (s)', yAxisIndex: 1, data: ans.map(function (a) { return a.seconds; }), showSymbol: ans.length < 40 }];
    draw('answers', b, ans.length ? null : 'No answers in the last 24 hours.');
    // Prompt size vs model context
    b = base();
    b.xAxis = { type: 'category', data: ans.map(function (a, i) { return i + 1; }), axisLabel: { show: false }, axisLine: { lineStyle: { color: css('--border', '#ccc') } } };
    b.yAxis = b._axis('tokens', '{value}', { min: 0, max: function (v) { return Math.max(v.max, data.num_ctx) * 1.05 | 0; } });
    b.series = [{ type: 'bar', name: 'Prompt tokens', data: ans.map(function (a) { return a.prompt; }), barMaxWidth: 14,
      markLine: { silent: true, symbol: 'none', data: [{ yAxis: data.num_ctx, name: 'Model context' }],
        label: { formatter: 'Model context ' + data.num_ctx, position: 'insideEndTop', color: css('--subtle', '#666') }, lineStyle: { color: css('--crit', '#db2100'), type: 'dashed' } } }];
    draw('prompt', b, ans.length ? null : 'No answers in the last 24 hours.');
    // Container CPU and memory
    b = base();
    b.yAxis = [b._axis('CPU %', '{value}', { min: 0 }), b._axis('MB', '{value}', { min: 0, splitLine: { show: false } })];
    b.series = [line('CPU', series('c_cpu'), 0, true), line('Memory (MB)', series('c_mem', 1048576), 1)];
    draw('c-load', b, hasData(['c_cpu', 'c_mem']) ? null : 'Collecting…');
    // Container I/O
    b = base();
    b.yAxis = b._axis('KB/s', '{value}', { min: 0 });
    b.series = [line('Net in', series('c_rx', 1024)), line('Net out', series('c_tx', 1024)), line('Disk read', series('c_rd', 1024)), line('Disk write', series('c_wr', 1024))];
    draw('c-io', b, hasData(['c_rx', 'c_rd']) ? null : 'Collecting…');
    // Observability container (only while it is running)
    var vNone = (data.now.vco || {}).running ? 'Collecting…' : 'Not running';
    b = base();
    b.yAxis = [b._axis('CPU %', '{value}', { min: 0 }), b._axis('MB', '{value}', { min: 0, splitLine: { show: false } })];
    b.series = [line('CPU', series('v_cpu'), 0, true), line('Memory (MB)', series('v_mem', 1048576), 1)];
    draw('v-load', b, hasData(['v_cpu', 'v_mem']) ? null : vNone);
    b = base();
    b.yAxis = b._axis('KB/s', '{value}', { min: 0 });
    b.series = [line('Net in', series('v_rx', 1024)), line('Net out', series('v_tx', 1024)), line('Disk read', series('v_rd', 1024)), line('Disk write', series('v_wr', 1024))];
    draw('v-io', b, hasData(['v_rx', 'v_rd']) ? null : vNone);
    // Power / temperature (macmon)
    var pw = hasData(['h_power', 'h_temp']);
    document.getElementById('perf-power-card').hidden = !pw;
    if (pw) {
      b = base();
      b.yAxis = [b._axis('W', '{value}', { min: 0 }), b._axis('°C', '{value}', { min: 0, splitLine: { show: false } })];
      b.series = [line('Power (W)', series('h_power'), 0, true), line('GPU temp (°C)', series('h_temp'), 1)];
      draw('power', b);
    }
  }

  // -- tables --------------------------------------------------------------------------
  function table(head, rows) {
    var t = el('table', 'datagrid');
    var tr = el('tr');
    head.forEach(function (h) { tr.appendChild(el('th', null, h)); });
    var th = el('thead'); th.appendChild(tr); t.appendChild(th);
    var tb = el('tbody');
    rows.forEach(function (r) { var row = el('tr'); r.forEach(function (c) { row.appendChild(el('td', null, c)); }); tb.appendChild(row); });
    t.appendChild(tb);
    return t;
  }
  function renderOllama() {
    var box = document.getElementById('perf-ollama'), o = data.now.ollama || {};
    box.innerHTML = '';
    if (!o.ok) { box.appendChild(el('p', 'subtle', 'Ollama did not answer: ' + (o.error || 'unknown error'))); return; }
    box.appendChild(el('p', 'subtle', 'Version ' + (o.version || '?') + ' · chat model ' + data.chat_model + '. A GPU share under 100% means part of the model runs on the CPU (much slower): use a smaller model or a smaller Model context.'));
    if (!o.models.length) { box.appendChild(el('p', null, 'No model is loaded right now. Ollama loads one with the next question.')); return; }
    box.appendChild(table(['Model', 'Size', 'Memory', 'On GPU', 'Context', 'Unloads'], o.models.map(function (m) {
      return [m.name, [m.params, m.quant].filter(Boolean).join(' '), bytes(m.size), m.gpu_pct == null ? '–' : m.gpu_pct + '%',
        m.context ? String(m.context) : '–', m.until ? new Date(m.until).toLocaleTimeString() : '–'];
    })));
  }
  function renderHost() {
    var box = document.getElementById('perf-host'), h = data.now.host;
    box.innerHTML = '';
    if (!h || !h.ok) { box.appendChild(el('p', 'subtle', 'No host agent. See the note at the top.')); return; }
    var x = h.host || {}, m = h.memory || {}, d = h.disk || {}, c = x.cores || {};
    var rows = [
      ['Host', [x.name, x.model].filter(Boolean).join(' · ')], ['Chip', x.chip || '–'], ['OS', x.os || '–'],
      ['Cores', c.p ? c.total + ' (' + c.p + ' performance, ' + c.e + ' efficiency)' : String(c.total || '–')],
      ['Load (1, 5, 15 min)', h.load ? h.load.join(', ') : '–'],
      ['Memory', bytes(m.used) + ' used of ' + bytes(m.total) + (m.app != null ? ' (app ' + bytes(m.app) + ', wired ' + bytes(m.wired) + ', compressed ' + bytes(m.compressed) + ')' : '')],
      ['Memory pressure', pct(m.pressure_pct)], ['Swap', bytes(m.swap_used) + ' of ' + bytes(m.swap_total)],
      ['GPU', h.gpu ? pct(h.gpu.pct) + (h.gpu.mem_used != null ? ' · ' + bytes(h.gpu.mem_used) + ' in use' : '') : 'not reported'],
      ['Ollama processes', (h.ollama ? h.ollama.processes + ' · ' + h.ollama.cpu_pct + '% CPU (one core = 100%) · ' + bytes(h.ollama.rss) : '–')],
      ['Disk (' + (d.path || '/') + ')', bytes(d.free) + ' free of ' + bytes(d.total)],
      ['Network', h.net ? bytes(h.net.rx_bps) + '/s in · ' + bytes(h.net.tx_bps) + '/s out' : '–'],
      ['Power and temperature', h.power && h.power.total_w != null ? h.power.total_w.toFixed(1) + ' W' + (h.temp && h.temp.gpu_c != null ? ' · GPU ' + h.temp.gpu_c.toFixed(0) + ' °C · CPU ' + (h.temp.cpu_c || 0).toFixed(0) + ' °C' : '') : 'Install macmon for these (brew install macmon)'],
      ['Agent', 'v' + h.version + ' · sampled ' + (h.age_seconds != null ? h.age_seconds + ' s' : '?') + ' before Ask read it' + (h.errors && h.errors.length ? ' · ' + h.errors.join('; ') : '')]
    ];
    var t = table(['', ''], rows);
    t.classList.add('perf-kv');
    t.querySelector('thead').remove();
    box.appendChild(t);
  }

  // -- web search usage (app/webusage.py) ---------------------------------------------
  function money(v) { return v == null ? '–' : '$' + (v < 0.01 && v > 0 ? v.toFixed(3) : v.toFixed(2)); }
  function renderWeb() {
    var w = data.web, box = document.getElementById('perf-web-kpis');
    if (!w || !box) return;
    box.innerHTML = '';
    var o = w.providers.ollama, b = w.providers.brave, q = w.brave_quota;
    box.appendChild(kpi('Ollama · ' + w.month, o.requests + (o.requests === 1 ? ' request' : ' requests'),
      money(o.cost) + ' estimated' + (o.failed ? ' · ' + o.failed + ' failed' : ''), o.limited ? 'warn' : 'ok'));
    box.appendChild(kpi('Brave · ' + w.month, b.requests + (b.requests === 1 ? ' request' : ' requests'),
      money(b.cost) + ' estimated' + (b.failed ? ' · ' + b.failed + ' failed' : ''), b.limited ? 'warn' : 'ok'));
    box.appendChild(kpi('Web search cost · ' + w.month, money(w.total_cost), 'estimated, both providers', 'ok'));
    if (q && q.remaining != null) {
      var used = q.limit ? 100 - q.remaining / q.limit * 100 : null;
      box.appendChild(kpi('Brave quota left', q.remaining.toLocaleString(), (q.limit ? 'of ' + q.limit.toLocaleString() + ' this month' : '') +
        (q.reset_at ? ' · resets ' + new Date(q.reset_at * 1000).toLocaleDateString() : ''), level(used, 80, 95) || 'ok'));
    }
    var any = w.days.some(function (d) { return w.series.some(function (s) { return d[s]; }) || d.failed; });
    var c = base();
    c.tooltip = { trigger: 'axis', confine: true, axisPointer: { type: 'shadow' }, formatter: function (ps) {
      var d = w.days[ps[0].dataIndex];
      return d.date + '<br>' + ps.map(function (p) { return p.marker + p.seriesName + ': ' + p.value; }).join('<br>') +
        '<br>Estimated cost: ' + money(d.cost);
    } };
    c.xAxis = { type: 'category', data: w.days.map(function (d) { return d.date.slice(5); }), axisLabel: { color: css('--subtle', '#5d6c74') } };
    c.yAxis = c._axis('Requests', '{value}', { minInterval: 1, min: 0 });
    c.series = w.series.map(function (s) { return { type: 'bar', stack: 'web', name: s, data: w.days.map(function (d) { return d[s]; }), barMaxWidth: 22 }; })
      .concat([{ type: 'bar', stack: 'web', name: 'Failed', data: w.days.map(function (d) { return d.failed; }), barMaxWidth: 22, itemStyle: { color: '#db2100' } }]);
    draw('web', c, any ? null : 'No web searches yet in ' + w.month + '.');
    var t = document.getElementById('perf-web-table');
    t.innerHTML = '';
    t.appendChild(table(['Tool', 'Requests ' + w.month, 'Failed', 'Price per call', 'Estimated cost'], w.tools.map(function (x) {
      return [x.label, String(x.ok), String(x.failed) + (x.limited ? ' (' + x.limited + ' limit)' : ''), money(x.price), money(x.cost)];
    })));
  }

  // -- Logs module (syslog server in the Observability container) -----------------------
  function num(n) { return n == null ? '–' : Number(n).toLocaleString(undefined, { maximumFractionDigits: 1 }); }
  function renderLogs() {
    var l = (data.now.vco || {}).logs;
    var card = document.getElementById('perf-logs-card');
    card.hidden = !l;
    if (!l) return;
    var box = document.getElementById('perf-logs-kpis');
    box.innerHTML = '';
    var capPct = l.max_size_bytes ? l.used_bytes / l.max_size_bytes * 100 : null;
    var diskPct = l.disk_total ? (l.disk_total - l.disk_free) / l.disk_total * 100 : null;
    box.appendChild(kpi('Logs per second', num(l.per_second), 'last 10 seconds, average', l.dropped_hour ? 'warn' : 'ok'));
    box.appendChild(kpi('Logs per minute', num(l.per_minute), 'the last 60 seconds'));
    box.appendChild(kpi('Logs per hour', num(l.per_hour), l.dropped_hour ? num(l.dropped_hour) + ' dropped in the last hour' : 'the last 60 minutes', l.dropped_hour ? 'warn' : ''));
    box.appendChild(kpi('Log disk space used', bytes(l.used_bytes), pct(capPct) + ' of the ' + bytes(l.max_size_bytes, 0) + ' cap',
      level(capPct, 80, 95)));
    box.appendChild(kpi('Disk space available', bytes(l.disk_free), 'free of ' + bytes(l.disk_total, 0) + ' (' + pct(diskPct) + ' used)', level(diskPct, 85, 95)));
    var GB = 1073741824, b = base();
    b.yAxis = [b._axis('per second', '{value}', { min: 0 }), b._axis('per minute', '{value}', { min: 0, splitLine: { show: false } })];
    b.series = [line('Per second', series('l_ps'), 0, true), line('Per minute', series('l_pm'), 1)];
    draw('logs-rate', b, hasData(['l_ps', 'l_pm']) ? null : 'Collecting…');
    b = base();
    b.yAxis = [b._axis('GB', '{value}', { min: 0 }), b._axis('GB', '{value}', { min: 0, splitLine: { show: false } })];
    b.series = [line('Log disk space used', series('l_used', GB), 0, true), line('Disk space available', series('l_free', GB), 1)];
    draw('logs-disk', b, hasData(['l_used', 'l_free']) ? null : 'Collecting…');
    var link = document.getElementById('perf-logs-link');
    if (data.observability_url && link.tagName !== 'A') {
      var a = el('a', null, 'Logs pages'); a.href = data.observability_url.replace(/\/$/, '') + '/dash/logs/search'; link.replaceWith(a); a.id = 'perf-logs-link';
    }
  }

  function render() {
    renderKpis(); renderAgentNote(); renderCharts(); renderLogs(); renderOllama(); renderHost(); renderWeb();
    document.getElementById('perf-updated').textContent = 'Updated ' + ago(data.now.ts) + ' · every ' + data.sample_seconds + ' s';
  }

  async function load() {
    try {
      var r = await fetch('/ui/perf?hours=' + hours, { cache: 'no-store' });
      if (!r.ok) throw new Error((await r.text()) || r.statusText);
      data = await r.json();
      render();
    } catch (e) {
      document.getElementById('perf-updated').textContent = 'Could not load: ' + e.message;
    }
  }

  document.querySelectorAll('#perf-range button').forEach(function (b) {
    b.classList.toggle('on', Number(b.dataset.h) === hours);
    b.addEventListener('click', function () {
      hours = Number(b.dataset.h);
      try { localStorage.setItem('ask.perf.hours', String(hours)); } catch (e) { /* private window */ }
      document.querySelectorAll('#perf-range button').forEach(function (x) { x.classList.toggle('on', x === b); });
      load();
    });
  });
  window.addEventListener('resize', function () { Object.keys(charts).forEach(function (k) { charts[k].resize(); }); });
  new MutationObserver(function () { if (data) renderCharts(); }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  load();
  setInterval(function () { if (!document.hidden) load(); }, 10000);
})();
