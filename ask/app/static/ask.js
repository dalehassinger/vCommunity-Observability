/* vCommunity Ask chat page: streams answers from /ui/chat (server-sent events),
   renders markdown with the vendored marked + DOMPurify, and shows source cards. */
(function () {
  'use strict';
  var thread = document.getElementById('thread');
  var form = document.getElementById('composer');
  var input = document.getElementById('question');
  // Sources picker: check boxes in a small menu. Nothing ticked = "all" (every source except
  // spreadsheets). A new chat starts with the last selection (kept in this browser only).
  var scopePick = document.getElementById('scope-pick');
  var scopeLabel = document.getElementById('scope-label');
  var scopeBoxes = scopePick ? Array.prototype.slice.call(scopePick.querySelectorAll('input[type=checkbox]')) : [];
  function scopeTicked() { return scopeBoxes.filter(function (b) { return b.checked; }); }
  function scopeValue() {
    var v = scopeTicked().map(function (b) { return b.value; });
    return v.length ? v : 'all';
  }
  function scopeRefresh(save) {
    if (!scopeLabel) return;
    var on = scopeTicked(), names = on.map(function (b) { return b.parentNode.textContent.trim(); });
    scopeLabel.textContent = !on.length ? 'All sources (not spreadsheets)' : on.length === 1 ? names[0] : on.length + ' sources';
    scopeLabel.title = on.length ? names.join(', ') : 'Every source except spreadsheets';
    if (save) { try { localStorage.setItem('ask.scope', JSON.stringify(on.map(function (b) { return b.value; }))); } catch (e) { /* private window */ } }
  }
  if (scopePick) {
    if (!(window.ASK && window.ASK.chatId) && !scopeTicked().length) {
      try {
        var last = JSON.parse(localStorage.getItem('ask.scope') || '[]');
        scopeBoxes.forEach(function (b) { b.checked = last.indexOf(b.value) >= 0; });
      } catch (e) { /* nothing saved */ }
    }
    scopeRefresh(false);
    scopePick.addEventListener('change', function () { scopeRefresh(true); });
    scopePick.addEventListener('click', function (e) {
      var t = e.target.closest('[data-scope-all],[data-scope-only]');
      if (!t) return;
      var only = t.getAttribute('data-scope-only');
      scopeBoxes.forEach(function (b) { b.checked = !!only && b.getAttribute('data-plugin') === only; });
      scopeRefresh(true);
    });
    document.addEventListener('click', function (e) { if (scopePick.open && !scopePick.contains(e.target)) scopePick.open = false; });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && scopePick.open) { scopePick.open = false; scopeLabel.focus(); } });
  }
  var sendBtn = document.getElementById('send');
  var stopBtn = document.getElementById('stop');
  var chatId = window.ASK.chatId || '';
  var controller = null;
  var msgSeq = 0;

  marked.setOptions({ gfm: true, breaks: false });

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // [2] or [1, 3] in the answer become links to the matching source card.
  function renderMarkdown(target, md, prefix) {
    var withCites = md.replace(/\[(\d{1,2}(?:\s*,\s*\d{1,2})*)\](?!\()/g, function (_, nums) {
      return nums.split(',').map(function (n) {
        n = n.trim();
        return '<a class="cite" href="#' + prefix + '-' + n + '" data-n="' + n + '">' + n + '</a>';
      }).join('');
    });
    target.innerHTML = DOMPurify.sanitize(marked.parse(withCites), { ADD_ATTR: ['data-n'] });
    target.querySelectorAll('a[href^="http"]').forEach(function (a) { a.target = '_blank'; a.rel = 'noopener'; });
  }

  function statusBadge(status) {
    if (!status) return null;
    var b = el('span', 'badge ' + (status === 'reviewed' ? 'badge-success' : 'badge-warn'), status);
    b.title = status === 'draft' ? 'Unreviewed content in the knowledge base' : status;
    return b;
  }

  // A PDF source's file (/files/<source>/<file>.pdf): open it at the cited page.
  function pdfLink(s) {
    if (!s.url || !/^\/files\/.+\.pdf$/i.test(s.url)) return null;
    var m = /^Page (\d+)/.exec(s.heading || '');
    return { href: s.url + (m ? '#page=' + m[1] : ''), label: 'Open PDF' + (m ? ', page ' + m[1] : '') + ' ↗' };
  }

  function linkLabel(url) {
    var host = '';
    try { host = new URL(url, location.href).hostname.replace(/^www\./, ''); } catch (e) { /* relative or bad URL */ }
    return host === 'github.com' ? 'View on GitHub ↗' : host ? 'View on ' + host + ' ↗' : 'View source ↗';
  }

  // Charts for data questions: Apache ECharts (vendored, loaded on first use). The chart is
  // built here from the spec the server made from the query result; nothing the model
  // wrote is run. Charts are redrawn when the theme changes and resized with the window.
  var charts = [];
  var echartsLoading = null;
  var ASSET_V = (document.currentScript && document.currentScript.src.split('?')[1]) || '';
  function loadECharts() {
    if (window.echarts) return Promise.resolve(window.echarts);
    if (!echartsLoading) echartsLoading = new Promise(function (resolve, reject) {
      var sc = document.createElement('script');
      sc.src = '/shared/vendor/echarts/echarts.min.js' + (ASSET_V ? '?' + ASSET_V : '');
      sc.onload = function () { resolve(window.echarts); };
      sc.onerror = function () { echartsLoading = null; reject(new Error('ECharts did not load')); };
      document.head.appendChild(sc);
    });
    return echartsLoading;
  }
  var cssVar = AskCharts.cssVar, chartOption = AskCharts.option;
  function drawChart(box, c) {
    loadECharts().then(function (ec) {
      var chart = ec.init(box, null, { renderer: 'canvas' });
      chart.setOption(chartOption(c, box._big));
      box._chart = chart; box._spec = c;
      charts.push(box);
      // Folded cards have no size yet: resize when the box becomes visible or changes width.
      if ('ResizeObserver' in window) new ResizeObserver(function () { chart.resize(); }).observe(box);
    }).catch(function () { box.textContent = 'The chart library could not be loaded.'; box.className += ' subtle'; });
  }
  window.addEventListener('resize', function () { charts.forEach(function (b) { if (b._chart) b._chart.resize(); }); });
  new MutationObserver(function () {
    charts.forEach(function (b) { if (b._chart) b._chart.setOption(chartOption(b._spec, b._big), true); });
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  // Enlarged chart: a full-window panel with the same chart; Esc, the close button or a
  // click outside closes it.
  function openChart(c) {
    var overlay = el('div', 'chart-overlay');
    var panel = el('div', 'chart-panel');
    var bar = el('div', 'chart-panel-bar');
    bar.appendChild(el('span', 'chart-panel-title', c.title));
    var png = el('button', 'link-btn', 'Save as PNG'); png.type = 'button';
    var close = el('button', 'icon-btn chart-close', '\u00d7'); close.type = 'button';
    close.setAttribute('aria-label', 'Close'); close.title = 'Close (Esc)';
    bar.appendChild(png); bar.appendChild(close);
    var box = el('div', 'chart-big');
    box._big = true;
    panel.appendChild(bar); panel.appendChild(box);
    if (c.note) panel.appendChild(el('div', 'subtle chart-note', c.note));
    overlay.appendChild(panel);
    document.body.appendChild(overlay);
    document.body.classList.add('chart-open');
    drawChart(box, c);
    png.onclick = function () { savePng(box); };
    function shut() {
      document.removeEventListener('keydown', onKey);
      if (box._chart) box._chart.dispose();
      charts = charts.filter(function (b) { return b !== box; });
      overlay.remove();
      document.body.classList.remove('chart-open');
    }
    function onKey(e) { if (e.key === 'Escape') shut(); }
    close.onclick = shut;
    overlay.addEventListener('click', function (e) { if (e.target === overlay) shut(); });
    document.addEventListener('keydown', onKey);
    close.focus();
  }
  function savePng(box) {
    if (!box._chart) return;
    var a = document.createElement('a');
    a.href = box._chart.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: cssVar('--surface', '#fff') });
    a.download = 'ask-chart.png'; document.body.appendChild(a); a.click(); a.remove();
  }

  // A data question's source: the SQL the model wrote and the rows it returned.
  // Links to the HTML report made for a report or dashboard question.
  function reportLinks(s) {
    var box = el('div', 'report-links');
    box.appendChild(el('span', 'report-ico', ''));
    var open = el('a', 'btn btn-primary', s.report.kind === 'dashboard' ? 'Open dashboard report' : s.report.kind === 'page' ? 'Open web page' : 'Open HTML report');
    open.href = s.report.url; open.target = '_blank'; open.rel = 'noopener';
    var dl = el('a', 'btn btn-outline', 'Download .html'); dl.href = s.report.url + '?download=1';
    var all = el('a', 'link-btn', 'All reports'); all.href = '/reports';
    box.appendChild(open); box.appendChild(dl); box.appendChild(all);
    // The report itself, inside the answer. Sandboxed without same-origin: its scripts
    // (charts, tables) run but cannot reach Ask, its cookies or the chat page.
    var wrap = el('div', 'report-preview');
    var frame = document.createElement('iframe');
    frame.setAttribute('sandbox', 'allow-scripts');
    frame.setAttribute('loading', 'lazy');
    frame.title = s.report.title || 'HTML report';
    frame.src = s.report.url + '#theme=' + (document.documentElement.getAttribute('data-theme') || 'light');
    wrap.appendChild(frame);
    var bar = el('div', 'report-preview-bar');
    var big = el('button', 'link-btn', 'Expand');
    big.type = 'button';
    big.addEventListener('click', function () {
      var on = wrap.classList.toggle('big');
      big.textContent = on ? 'Shrink' : 'Expand';
      if (on) wrap.scrollIntoView({ block: 'start', behavior: 'smooth' });
    });
    var hide = el('button', 'link-btn', 'Hide preview');
    hide.type = 'button';
    hide.addEventListener('click', function () {
      var off = wrap.classList.toggle('off');
      hide.textContent = off ? 'Show preview' : 'Hide preview';
      big.hidden = off;
    });
    bar.appendChild(big); bar.appendChild(hide);
    var outer = el('div', 'report-block');
    outer.appendChild(box); outer.appendChild(bar); outer.appendChild(wrap);
    return outer;
  }

  // A dashboard question's card: KPI tiles and small versions of the panels.
  function dashboardCard(s, prefix) {
    var card = el('div', 'src-card data');
    card.id = prefix + '-' + s.n;
    var head = el('div', 'src-head');
    head.appendChild(el('span', 'src-n', String(s.n)));
    head.appendChild(el('span', 'src-title', s.title));
    card.appendChild(head);
    var meta = el('div', 'src-meta');
    meta.appendChild(el('code', null, s.doc_id));
    meta.appendChild(el('span', 'badge', s.panels.length + ' panels'));
    card.appendChild(meta);
    if (s.report) card.appendChild(reportLinks(s));
    var kpis = s.panels.filter(function (p) { return p.kind === 'kpi'; });
    if (kpis.length) {
      var row = el('div', 'dash-kpis');
      kpis.forEach(function (p) {
        var k = el('div', 'dash-kpi');
        k.appendChild(el('div', 'dash-kpi-label', p.title));
        k.appendChild(el('div', 'dash-kpi-value', typeof p.value === 'number' ? p.value.toLocaleString() : String(p.value)));
        row.appendChild(k);
      });
      card.appendChild(row);
    }
    var grid = el('div', 'dash-grid');
    s.panels.filter(function (p) { return p.kind !== 'kpi'; }).forEach(function (p) {
      var cell = el('div', 'dash-cell' + (p.chart ? '' : ' wide'));
      cell.appendChild(el('div', 'dash-cell-title', p.title));
      if (p.chart) {
        var box = el('div', 'chart-box small');
        cell.appendChild(box);
        var big = el('button', 'icon-btn chart-expand'); big.type = 'button'; big.title = 'Enlarge chart';
        big.setAttribute('aria-label', 'Enlarge chart');
        big.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
        big.onclick = function () { openChart(p.chart); };
        cell.appendChild(big);
        requestAnimationFrame(function () { drawChart(box, Object.assign({}, p.chart, { title: '' })); });
      } else {
        var wrap = el('div', 'data-table-wrap'), t = el('table', 'datagrid compact'), tr = el('tr');
        p.columns.forEach(function (c) { tr.appendChild(el('th', null, c)); });
        var th = el('thead'); th.appendChild(tr); t.appendChild(th);
        var tb = el('tbody');
        p.rows.slice(0, 10).forEach(function (r) {
          var row = el('tr');
          r.forEach(function (v) { row.appendChild(el('td', typeof v === 'number' ? 'num' : null, v == null ? '' : (typeof v === 'number' && !Number.isInteger(v) ? String(Math.round(v * 100) / 100) : String(v)))); });
          tb.appendChild(row);
        });
        t.appendChild(tb); wrap.appendChild(t); cell.appendChild(wrap);
      }
      grid.appendChild(cell);
    });
    card.appendChild(grid);
    var det = el('details', 'dash-sql');
    det.appendChild(el('summary', null, 'Queries (' + s.panels.length + ')' + (s.skipped && s.skipped.length ? ', ' + s.skipped.length + ' left out' : '')));
    s.panels.forEach(function (p) {
      det.appendChild(el('div', 'dash-cell-title', p.title));
      var pre = el('pre', 'sql'); pre.appendChild(el('code', null, p.sql)); det.appendChild(pre);
    });
    (s.skipped || []).forEach(function (p) {
      det.appendChild(el('div', 'dash-cell-title', p.title + ' (left out: ' + p.error + ')'));
      var pre = el('pre', 'sql'); pre.appendChild(el('code', null, p.sql)); det.appendChild(pre);
    });
    card.appendChild(det);
    return card;
  }

  function dataCard(s, prefix) {
    if (s.panels && s.panels.length) return dashboardCard(s, prefix);
    var card = el('div', 'src-card data');
    card.id = prefix + '-' + s.n;
    var head = el('div', 'src-head');
    head.appendChild(el('span', 'src-n', String(s.n)));
    head.appendChild(el('span', 'src-title', s.title));
    card.appendChild(head);
    var meta = el('div', 'src-meta');
    meta.appendChild(el('code', null, s.doc_id));
    meta.appendChild(el('span', 'badge', s.row_count + (s.truncated ? '+' : '') + ' row' + (s.row_count === 1 ? '' : 's')));
    card.appendChild(meta);
    if (s.report) card.appendChild(reportLinks(s));
    var chartBox = null;
    if (s.chart && s.chart.categories && s.chart.categories.length) {
      var wrapC = el('div', 'chart-wrap');
      chartBox = el('div', 'chart-box');
      chartBox.setAttribute('role', 'img');
      chartBox.setAttribute('aria-label', s.chart.type + ' chart: ' + s.chart.title);
      var expand = el('button', 'icon-btn chart-expand'); expand.type = 'button';
      expand.title = 'Enlarge chart'; expand.setAttribute('aria-label', 'Enlarge chart');
      expand.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      expand.onclick = function () { openChart(s.chart); };
      wrapC.appendChild(chartBox); wrapC.appendChild(expand);
      card.appendChild(wrapC);
      if (s.chart.note) card.appendChild(el('div', 'subtle chart-note', s.chart.note));
      // Draw once the card is in the page (ECharts needs the box's size).
      requestAnimationFrame(function () { drawChart(chartBox, s.chart); });
    }
    var pre = el('pre', 'sql'); pre.appendChild(el('code', null, s.sql)); card.appendChild(pre);
    if (s.columns && s.columns.length && s.rows && s.rows.length) {
      var wrap = el('div', 'data-table-wrap');
      var t = el('table', 'datagrid compact');
      var first = s.rows[0] || [];
      var tr = el('tr'); s.columns.forEach(function (c, i) { tr.appendChild(el('th', typeof first[i] === 'number' ? 'num' : null, c)); });
      var thead = el('thead'); thead.appendChild(tr); t.appendChild(thead);
      var tb = el('tbody');
      s.rows.forEach(function (r) {
        var row = el('tr');
        r.forEach(function (v) {
          if (typeof v === 'number' && !Number.isInteger(v)) v = Math.round(v * 100) / 100;
          row.appendChild(el('td', typeof v === 'number' ? 'num' : null, v == null ? '' : String(v)));
        });
        tb.appendChild(row);
      });
      t.appendChild(tb); wrap.appendChild(t); card.appendChild(wrap);
      if (s.truncated) card.appendChild(el('div', 'subtle', 'The first ' + s.rows.length + ' rows are shown (the query limit).'));
    }
    var copy = el('button', 'link-btn', 'Copy SQL'); copy.type = 'button';
    copy.onclick = function () { try { navigator.clipboard.writeText(s.sql); copy.textContent = 'Copied'; } catch (e) {} };
    var links = el('div', 'src-links'); links.appendChild(copy);
    if (s.columns && s.columns.length && s.rows && s.rows.length) {
      var dl = el('button', 'link-btn', 'Download CSV'); dl.type = 'button';
      dl.onclick = function () {
        var q = function (v) { v = v == null ? '' : String(v); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
        var csv = [s.columns.map(q).join(',')].concat(s.rows.map(function (r) { return r.map(q).join(','); })).join('\r\n');
        var a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
        a.download = 'ask-query.csv'; document.body.appendChild(a); a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
      };
      links.appendChild(dl);
    }
    if (chartBox) {
      var png = el('button', 'link-btn', 'Save chart as PNG'); png.type = 'button';
      png.onclick = function () { savePng(chartBox); };
      links.appendChild(png);
      var big = el('button', 'link-btn', 'Enlarge chart'); big.type = 'button';
      big.onclick = function () { openChart(s.chart); };
      links.appendChild(big);
    }
    card.appendChild(links);
    return card;
  }

  // A whole-source question's source: the catalog of every post in one source, with the
  // counts Ask made (posts, dates, posts per year, frequent title words) and the full list.
  function catalogCard(s, prefix) {
    var card = el('div', 'src-card catalog-card');
    card.id = prefix + '-' + s.n;
    var head = el('div', 'src-head');
    head.appendChild(el('span', 'src-n', String(s.n)));
    var t = el('a', 'src-title', s.title);
    t.href = '/sources/' + encodeURIComponent(s.source);
    head.appendChild(t);
    card.appendChild(head);
    var kpis = el('div', 'cat-kpis');
    [['Posts', String(s.posts)], ['First', s.first || '–'], ['Latest', s.last || '–']].concat(
      s.other ? [['Other pages', String(s.other)]] : []).forEach(function (k) {
      var d = el('div', 'cat-kpi');
      d.appendChild(el('span', 'cat-kpi-label', k[0]));
      d.appendChild(el('b', null, k[1]));
      kpis.appendChild(d);
    });
    card.appendChild(kpis);
    if (s.years && s.years.length) {
      var max = Math.max.apply(null, s.years.map(function (y) { return y[1]; })) || 1;
      var yrs = el('div', 'cat-years');
      s.years.forEach(function (y) {
        var row = el('div', 'cat-year');
        row.appendChild(el('span', null, y[0]));
        var bar = el('span', 'cat-bar'); var fill = el('i'); fill.style.width = Math.round(y[1] / max * 100) + '%';
        bar.appendChild(fill); row.appendChild(bar);
        row.appendChild(el('b', null, String(y[1])));
        yrs.appendChild(row);
      });
      card.appendChild(yrs);
    }
    if (s.words && s.words.length) {
      var w = el('div', 'cat-words');
      w.appendChild(el('span', 'subtle', 'Frequent in titles: '));
      s.words.forEach(function (x) { w.appendChild(el('span', 'badge', x[0] + ' · ' + x[1])); });
      card.appendChild(w);
    }
    var det = el('details', 'cat-list');
    det.appendChild(el('summary', null, 'All ' + s.items.length + ' posts' +
      (s.shown < s.items.length ? ' (the model saw the newest ' + s.shown + ')' : '')));
    var ol = el('ol');
    s.items.forEach(function (it) {
      var li = el('li');
      li.appendChild(el('span', 'cat-date', it.date || 'undated'));
      var a = el('a', null, it.title);
      a.href = '/doc/' + it.row;
      li.appendChild(a);
      if (it.url) { var ext = el('a', 'cat-ext', '↗'); ext.href = it.url; ext.target = '_blank'; ext.rel = 'noopener'; ext.title = it.url; li.appendChild(ext); }
      ol.appendChild(li);
    });
    det.appendChild(ol);
    card.appendChild(det);
    var links = el('div', 'src-links');
    var open = el('a', null, 'Source documents'); open.href = '/sources/' + encodeURIComponent(s.source);
    links.appendChild(open);
    if (s.url) { var pl = pdfLink(s), v = el('a', null, pl ? pl.label : linkLabel(s.url)); v.href = pl ? pl.href : s.url; v.target = '_blank'; v.rel = 'noopener'; links.appendChild(v); }
    card.appendChild(links);
    return card;
  }

  // One MCP tool call: what ran, with which arguments, and what came back. A tool set to
  // "Ask first" shows Run / Cancel here until the reader answers.
  var TOOL_STATUS = { ok: ['done', 'badge-success'], error: ['failed', 'badge-danger'], running: ['running…', 'badge-info'],
    waiting: ['needs your OK', 'badge-warn'], cancelled: ['cancelled', ''], refused: ['not run', ''] };
  function toolCard(s, prefix) {
    var card = el('div', 'src-card tool-card ' + (s.status || ''));
    card.id = prefix + '-' + s.n;
    var head = el('div', 'src-head');
    head.appendChild(el('span', 'src-n', String(s.n)));
    head.appendChild(el('code', 'tool-name', s.tool || '?'));
    card.appendChild(head);
    var meta = el('div', 'src-meta');
    var st = TOOL_STATUS[s.status] || [s.status || '', ''];
    meta.appendChild(el('span', 'badge ' + st[1], st[0]));
    if (s.server) meta.appendChild(el('span', 'badge', s.server));
    if (s.read_only === false) { var w = el('span', 'badge badge-warn', 'changes things'); w.title = 'The MCP server says this tool is not read-only'; meta.appendChild(w); }
    if (s.ms != null) meta.appendChild(el('span', 'subtle', (s.ms >= 1000 ? (s.ms / 1000).toFixed(1) + ' s' : s.ms + ' ms')));
    card.appendChild(meta);
    var args = s.arguments || {};
    if (Object.keys(args).length) card.appendChild(el('pre', 'tool-args', JSON.stringify(args, null, 2)));
    else card.appendChild(el('div', 'subtle tool-noargs', 'No arguments'));
    if (s.status === 'waiting' && s.approve) {
      var box = el('div', 'tool-confirm');
      box.appendChild(el('span', null, 'Run this tool with these arguments?'));
      var run = el('button', 'btn btn-primary btn-sm', 'Run'), no = el('button', 'btn btn-outline btn-sm', 'Cancel');
      run.type = no.type = 'button';
      function send(yes) {
        run.disabled = no.disabled = true;
        fetch('/ui/mcp/confirm', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: s.approve, run: yes }) })
          .then(function (r) { if (!r.ok) return r.json().then(function (d) { throw new Error(d.detail || r.statusText); }); })
          .catch(function (e) { box.appendChild(el('span', 'tool-err', e.message)); });
      }
      run.onclick = function () { send(true); };
      no.onclick = function () { send(false); };
      box.appendChild(run); box.appendChild(no);
      card.appendChild(box);
    }
    if (s.note) card.appendChild(el('div', 'subtle chart-note', s.note));
    if (s.error) card.appendChild(el('div', 'tool-err', s.error));
    if (s.links && s.links.length) {
      // Web search / fetch: the pages found, as links.
      var ul = el('ul', 'tool-links');
      s.links.forEach(function (l) {
        var li = el('li'), a = el('a', null, l.title || l.url);
        a.href = l.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; a.title = l.url;
        li.appendChild(a);
        try { li.appendChild(el('span', 'subtle', ' · ' + new URL(l.url).hostname.replace(/^www\./, ''))); } catch (e) { /* bad URL */ }
        ul.appendChild(li);
      });
      card.appendChild(ul);
    }
    if (s.result != null) {
      var det = el('details', 'tool-result');
      det.appendChild(el('summary', null, 'Result' + (s.chars ? ' (' + s.chars.toLocaleString() + ' characters' + (s.cut ? ', the model read the first part' : '') + ')' : '')));
      det.appendChild(el('pre', null, s.result));
      card.appendChild(det);
    }
    return card;
  }

  function sourceCard(s, prefix) {
      if (s.kind === 'tool') return toolCard(s, prefix);
      if (s.kind === 'data') return dataCard(s, prefix);
      if (s.kind === 'catalog') return catalogCard(s, prefix);
      var card = el('div', 'src-card ' + (s.kind === 'linked' ? 'linked' : 'hit'));
      card.id = prefix + '-' + s.n;
      var head = el('div', 'src-head');
      head.appendChild(el('span', 'src-n', String(s.n)));
      var t = el('a', 'src-title', s.title);
      t.href = '/doc/' + s.doc_row;
      head.appendChild(t);
      card.appendChild(head);
      if (s.heading) card.appendChild(el('div', 'src-heading', s.heading));
      var meta = el('div', 'src-meta');
      meta.appendChild(el('code', null, s.doc_id));
      var sb = statusBadge(s.status);
      if (sb) meta.appendChild(sb);
      if (s.date) { var db_ = el('span', 'badge', s.date); db_.title = 'Published'; meta.appendChild(db_); }
      if (s.kind === 'linked') {
        var lb = el('span', 'badge', 'linked · ' + s.via.replace('_', ' '));
        lb.title = 'Added because ' + s.from_doc + ' links to it (' + s.via + ')';
        meta.appendChild(lb);
      }
      card.appendChild(meta);
      if (s.snippet) card.appendChild(el('p', 'src-snippet', s.snippet));
      if (s.images && s.images.length) {
        var imgs = el('div', 'src-images');
        s.images.forEach(function (im) {
          var a = el('a');
          a.href = im.url; a.target = '_blank'; a.rel = 'noopener'; a.title = im.alt || 'Diagram';
          var img = el('img');
          img.src = im.url; img.alt = im.alt || ''; img.loading = 'lazy';
          a.appendChild(img);
          imgs.appendChild(a);
        });
        card.appendChild(imgs);
      }
      var links = el('div', 'src-links');
      var open = el('a', null, 'Open'); open.href = '/doc/' + s.doc_row;
      links.appendChild(open);
      if (s.url) {
        var pl = pdfLink(s), gh = el('a', null, pl ? pl.label : linkLabel(s.url)); gh.href = pl ? pl.href : s.url; gh.target = '_blank'; gh.rel = 'noopener';
        links.appendChild(gh);
      }
      card.appendChild(links);
      return card;
      }

  // Sources: a compact strip of numbered chips above the answer (stays small while the
  // answer streams), and the full cards folded away under the answer.
  function sourceCards(container, sources, prefix, strip) {
    container.innerHTML = '';
    if (strip) strip.innerHTML = '';
    if (!sources || !sources.length) return;
    // A web page the model wrote: shown first, right under the answer text.
    sources.filter(function (s) { return s.kind === 'page' && s.report; }).forEach(function (s) {
      container.appendChild(reportLinks(s));
    });
    var tools = sources.filter(function (s) { return s.kind === 'tool'; });
    var hits = sources.filter(function (s) { return s.kind !== 'linked' && s.kind !== 'tool' && s.kind !== 'page'; });
    var linked = sources.filter(function (s) { return s.kind === 'linked'; });
    if (strip && (hits.length || tools.length || linked.length)) {
      strip.appendChild(el('span', 'strip-label', 'Sources'));
      hits.forEach(function (s) {
        var c = el('a', 'src-chip');
        c.href = '#' + prefix + '-' + s.n;
        c.dataset.card = prefix + '-' + s.n;
        c.title = s.title + (s.heading ? ' > ' + s.heading : '') + '\n' + s.doc_id;
        c.appendChild(el('b', null, String(s.n)));
        c.appendChild(el('span', null, s.title + (s.heading ? ' · ' + s.heading : '')));
        strip.appendChild(c);
      });
      tools.forEach(function (s) {
        var c = el('a', 'src-chip tool ' + (s.status || ''));
        c.href = '#' + prefix + '-' + s.n;
        c.dataset.card = prefix + '-' + s.n;
        c.title = 'Tool call: ' + (s.tool || '') + (s.server ? ' (' + s.server + ')' : '') + ' · ' + (TOOL_STATUS[s.status] || [s.status])[0];
        c.appendChild(el('b', null, String(s.n)));
        c.appendChild(el('span', null, s.tool || '?'));
        strip.appendChild(c);
      });
      if (linked.length) {
        var more = el('a', 'src-chip more', '+' + linked.length + ' linked');
        more.href = '#' + prefix + '-' + linked[0].n;
        more.dataset.card = prefix + '-' + linked[0].n;
        more.title = 'Files the matches link to (same_name, internal_derive, related)';
        strip.appendChild(more);
      }
    }
    var det = el('details', 'sources');
    var isData = hits.length && hits[0].kind === 'data';
    var isCat = hits.length && hits[0].kind === 'catalog';
    det.appendChild(el('summary', null, isData ? 'Query and result' : isCat ? 'Catalog and sources (' + hits.length + ')' :
      'Source details (' + hits.length + (linked.length ? ' + ' + linked.length + ' linked' : '') + ')'));
    // Only a data result starts open (it is the answer). Catalog and sources start folded so the reply stays in view.
    if (isData) det.open = true;
    [[hits, ''], [linked, 'Linked files']].forEach(function (g) {
      if (!g[0].length) return;
      if (g[1]) det.appendChild(el('div', 'src-group', g[1]));
      var grid = el('div', 'src-grid');
      g[0].forEach(function (s) { grid.appendChild(sourceCard(s, prefix)); });
      det.appendChild(grid);
    });
    if (hits.length || linked.length) container.appendChild(det);
    if (tools.length) {
      // Tool calls: folded like the sources, but open while a tool waits for Run or Cancel.
      var td = el('details', 'sources tool-calls');
      td.appendChild(el('summary', null, 'Tool calls (' + tools.length + ')'));
      td.open = tools.some(function (s) { return s.status === 'waiting' && s.approve; });
      var tg = el('div', 'src-grid tool-grid');
      tools.forEach(function (s) { tg.appendChild(toolCard(s, prefix)); });
      td.appendChild(tg);
      container.appendChild(td);
    }
  }

  function statsLine(container, m) {
    container.innerHTML = '';
    var parts = [];
    if (m.model) parts.push(m.model);
    var st = m.stats || {};
    if (st.seconds != null) parts.push(st.seconds + ' s');
    if (st.tok_per_s) parts.push(st.tok_per_s + ' tokens/s');
    container.appendChild(el('span', null, parts.join(' · ')));
    var copy = el('button', 'link-btn', 'Copy answer');
    copy.type = 'button';
    copy.onclick = function () {
      try { navigator.clipboard.writeText(m.content || ''); copy.textContent = 'Copied'; } catch (e) {}
      setTimeout(function () { copy.textContent = 'Copy answer'; }, 1500);
    };
    container.appendChild(copy);
  }

  function addUser(text) {
    var row = el('div', 'msg user');
    row.appendChild(el('div', 'bubble', text));
    thread.appendChild(row);
    return row;
  }

  function addAssistant() {
    var prefix = 'm' + (++msgSeq);
    var row = el('div', 'msg assistant');
    var avatar = el('div', 'avatar');
    avatar.innerHTML = '<svg viewBox="0 0 32 32" width="18" height="18"><path d="M8 10.5h16a1.5 1.5 0 0 1 1.5 1.5v8a1.5 1.5 0 0 1-1.5 1.5h-8l-5 3.5v-3.5H8A1.5 1.5 0 0 1 6.5 20v-8A1.5 1.5 0 0 1 8 10.5z" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linejoin="round"/></svg>';
    var body = el('div', 'answer');
    var thinking = el('details', 'thinking');
    thinking.hidden = true;
    thinking.appendChild(el('summary', null, 'Thinking'));
    var thinkPre = el('div', 'think-text');
    thinking.appendChild(thinkPre);
    var strip = el('div', 'src-strip');
    var text = el('div', 'md');
    var sources = el('div', 'src-wrap');
    var stats = el('div', 'answer-stats');
    body.appendChild(strip); body.appendChild(thinking); body.appendChild(text); body.appendChild(sources); body.appendChild(stats);
    row.appendChild(avatar); row.appendChild(body);
    thread.appendChild(row);
    return { row: row, strip: strip, text: text, sources: sources, stats: stats, thinking: thinking, thinkPre: thinkPre, prefix: prefix };
  }

  function renderSaved(m) {
    if (m.role === 'user') { addUser(m.content); return; }
    var a = addAssistant();
    if (m.thinking) { a.thinking.hidden = false; a.thinkPre.textContent = m.thinking; }
    renderMarkdown(a.text, m.content, a.prefix);
    sourceCards(a.sources, m.sources, a.prefix, a.strip);
    if (m.stats && m.stats.error) a.row.classList.add('failed');
    statsLine(a.stats, m);
  }

  // Follow the answer as it streams, unless the reader scrolled up to read something.
  var follow = true;
  window.addEventListener('wheel', function (e) { if (e.deltaY < 0) follow = false; }, { passive: true });
  window.addEventListener('touchmove', function () { follow = false; }, { passive: true });
  window.addEventListener('scroll', function () {
    if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 80) follow = true;
  }, { passive: true });
  function scrollDown(force) {
    if (force) follow = true;
    if (follow) window.scrollTo(0, document.body.scrollHeight);
  }

  function busy(on) {
    sendBtn.hidden = on; stopBtn.hidden = !on; input.disabled = on;
    if (!on) input.focus();
  }

  function addSidebarChat(id, title) {
    var nav = document.querySelector('.sidenav');
    var empty = nav.querySelector('.nav-empty');
    if (empty) empty.remove();
    var list = document.getElementById('chat-list');
    var a = el('a', 'nav-link sub chat-link active', title);
    a.href = '/chat/' + id; a.title = title;
    nav.querySelectorAll('.nav-link.active').forEach(function (x) { x.classList.remove('active'); });
    if (!list) return;
    // Same markup as base.html, so the sidebar's Select mode can tick it too.
    var row = el('div', 'chat-row'), box = el('input', 'chat-check');
    box.type = 'checkbox'; box.value = id; box.tabIndex = -1;
    box.setAttribute('aria-label', 'Select chat: ' + title);
    row.appendChild(box); row.appendChild(a);
    list.insertBefore(row, list.firstChild);
    var sel = document.getElementById('chat-select');
    if (sel && document.getElementById('chat-bar').hidden) sel.hidden = false;
  }

  async function ask(question) {
    var empty = document.getElementById('empty-state');
    if (empty) empty.remove();
    addUser(question);
    var a = addAssistant();
    a.text.innerHTML = '<span class="typing"><i></i><i></i><i></i></span> <span class="subtle">Searching the knowledge sources…</span>';
    scrollDown(true);
    busy(true);
    controller = new AbortController();
    var content = '', thinking = '', meta = {}, pending = false, isNew = !chatId;
    function paint() {
      pending = false;
      renderMarkdown(a.text, content || '', a.prefix);
      if (thinking) a.thinkPre.textContent = thinking;
    }
    try {
      var res = await fetch('/ui/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
        body: JSON.stringify({ chat_id: chatId, question: question, scope: scopeValue() })
      });
      if (!res.ok) throw new Error((await res.text()) || res.statusText);
      var reader = res.body.getReader(), dec = new TextDecoder(), buf = '';
      while (true) {
        var r = await reader.read();
        if (r.done) break;
        buf += dec.decode(r.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          var raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
          var ev = 'message', data = '';
          raw.split('\n').forEach(function (line) {
            if (line.indexOf('event: ') === 0) ev = line.slice(7);
            else if (line.indexOf('data: ') === 0) data += line.slice(6);
          });
          var payload = data ? JSON.parse(data) : null;
          if (ev === 'meta') {
            meta = payload;
            if (isNew && payload.chat_id) {
              chatId = payload.chat_id;
              history.replaceState(null, '', '/chat/' + chatId);
              document.getElementById('chat-title').textContent = question.slice(0, 120);
              document.getElementById('delete-chat').hidden = false;
              addSidebarChat(chatId, question.slice(0, 120));
            }
            sourceCards(a.sources, payload.sources, a.prefix, a.strip);
            a.text.innerHTML = '<span class="typing"><i></i><i></i><i></i></span> <span class="subtle">' +
              (payload.tools ? (payload.sources.length ? 'Found ' + payload.sources.length + ' sources and ' : '') + payload.tools + ' tools, asking ' :
               payload.sources.length && payload.sources[0].kind === 'data' ? 'Query returned ' + payload.sources[0].row_count + ' rows, asking ' :
               payload.sources.length && payload.sources[0].kind === 'catalog' ? 'Catalog of ' + payload.sources.filter(function (s) { return s.kind === 'catalog'; })
                 .reduce(function (t, s) { return t + s.posts; }, 0) + ' posts, asking ' :
               payload.sources.length ? 'Found ' + payload.sources.length + ' sources, asking ' : 'No matching sources, asking ') +
              DOMPurify.sanitize(payload.model) + '…</span>';
          } else if (ev === 'tool') {
            meta.sources = (meta.sources || []).filter(function (s) { return s.n !== payload.n; }).concat([payload])
              .sort(function (x, y) { return x.n - y.n; });
            sourceCards(a.sources, meta.sources, a.prefix, a.strip);
            if (!content) {
              a.text.innerHTML = '';
              var tl = el('span', 'subtle');
              if (payload.status === 'waiting') {
                tl.textContent = 'Waiting for you: Run or Cancel ' + payload.tool + ' (under Tool calls).';
                var go = a.sources.querySelector('.tool-confirm');
                if (go) setTimeout(function () { go.scrollIntoView({ behavior: 'smooth', block: 'center' }); }, 50);
              } else {
                a.text.innerHTML = '<span class="typing"><i></i><i></i><i></i></span> ';
                tl.textContent = payload.status === 'running' ? 'Running ' + payload.tool + '…' : 'Asking ' + (meta.model || 'the model') + '…';
              }
              a.text.appendChild(tl);
            }
          } else if (ev === 'page') {
            content = payload.content || content;
            paint();
            meta.sources = (meta.sources || []).concat([payload.card]);
            sourceCards(a.sources, meta.sources, a.prefix, a.strip);
          } else if (ev === 'report') {
            if (meta && meta.sources && meta.sources[0]) {
              meta.sources[0].report = payload;
              sourceCards(a.sources, meta.sources, a.prefix, a.strip);
            }
          } else if (ev === 'thinking') {
            thinking += payload; a.thinking.hidden = false;
            if (!pending) { pending = true; requestAnimationFrame(paint); }
          } else if (ev === 'token') {
            content += payload;
            if (!pending) { pending = true; requestAnimationFrame(paint); }
          } else if (ev === 'done') {
            paint();
            statsLine(a.stats, { model: payload.model, stats: payload.stats, content: content });
          } else if (ev === 'error') {
            if (payload.chat_id && !chatId) chatId = payload.chat_id;
            a.row.classList.add('failed');
            a.text.innerHTML = '';
            a.text.appendChild(el('div', 'alert alert-danger', payload.message));
          }
        }
        scrollDown();
      }
    } catch (e) {
      if (e.name === 'AbortError') {
        paint();
        a.stats.textContent = 'Stopped';
      } else {
        a.row.classList.add('failed');
        a.text.innerHTML = '';
        a.text.appendChild(el('div', 'alert alert-danger', 'Request failed: ' + e.message));
      }
    } finally {
      controller = null;
      busy(false);
    }
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var q = input.value.trim();
    if (!q || controller) return;
    input.value = ''; autosize();
    ask(q);
  });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  function autosize() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 220) + 'px'; }
  input.addEventListener('input', autosize);
  stopBtn.addEventListener('click', function () { if (controller) controller.abort(); });
  document.querySelectorAll('.example').forEach(function (b) {
    b.addEventListener('click', function () { input.value = b.dataset.q; form.requestSubmit(); });
  });
  thread.addEventListener('click', function (e) {
    var c = e.target.closest('a.cite, a.src-chip');
    if (!c) return;
    var card = document.querySelector(c.getAttribute('href'));
    if (card) {
      e.preventDefault();
      for (var d = card.closest('details'); d; d = d.parentElement && d.parentElement.closest('details')) d.open = true;
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      card.classList.add('flash'); setTimeout(function () { card.classList.remove('flash'); }, 1200);
    }
  });
  document.getElementById('delete-chat').addEventListener('click', async function () {
    if (!chatId || !confirm('Delete this chat?')) return;
    await fetch('/ui/chats/' + chatId + '/delete', { method: 'POST' });
    location.href = '/chat';
  });

  (window.ASK.messages || []).forEach(renderSaved);
  if (window.ASK.messages && window.ASK.messages.length) scrollDown();
  input.focus();
})();
