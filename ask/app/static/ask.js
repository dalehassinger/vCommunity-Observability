/* vCommunity Ask chat page: streams answers from /ui/chat (server-sent events),
   renders markdown with the vendored marked + DOMPurify, and shows source cards. */
(function () {
  'use strict';
  // Smooth scrolling only when the OS "reduce motion" setting is off.
  function scrollMode() { return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'; }
  var thread = document.getElementById('thread');
  var form = document.getElementById('composer');
  var input = document.getElementById('question');
  // Sources picker: buttons under the prompt, grouped by kind (each is a check box styled as a
  // button). Nothing picked = "all" (every source except spreadsheets). A new chat starts with
  // the last selection, and the list can be folded (both kept in this browser only).
  var scopePick = document.getElementById('scope-pick');
  // The picking rules live in static/scope.js (shared with the Saved and Scheduled prompts forms).
  var scope = scopePick ? AskScope.bind(scopePick, {
    label: document.getElementById('scope-label'),
    onChange: function (byUser) {
      // A pick by hand is remembered for the next new chat (a saved prompt's pick is not).
      if (byUser) { try { localStorage.setItem('ask.scope', JSON.stringify(scope.picked())); } catch (e) { /* private window */ } }
    }
  }) : null;
  var scopeBoxes = scope ? scope.boxes() : [];
  function scopeValue() { return scope ? scope.value() : 'all'; }
  function scopeRefresh(save) { if (scope) scope.refresh(save); }
  if (scopePick) {
    if (!(window.ASK && window.ASK.chatId) && !scope.picked().length) {
      try { scope.set(JSON.parse(localStorage.getItem('ask.scope') || '[]')); } catch (e) { /* nothing saved */ }
    }
    scopeRefresh(false);
    var fold = document.getElementById('scope-fold');
    if (fold) {
      var setFold = function (folded, save) {
        scopePick.classList.toggle('folded', folded);
        fold.setAttribute('aria-expanded', folded ? 'false' : 'true');
        fold.textContent = folded ? 'Show list' : 'Hide list';
        if (save) { try { localStorage.setItem('ask.scope.folded', folded ? '1' : '0'); } catch (e) { /* private window */ } }
      };
      // Folded on a phone unless the reader chose otherwise: the list would cover the chat.
      var folded0 = window.matchMedia('(max-width: 600px)').matches;
      try { var f = localStorage.getItem('ask.scope.folded'); if (f !== null) folded0 = f === '1'; } catch (e) { /* private window */ }
      setFold(folded0, false);
      fold.addEventListener('click', function () { setFold(!scopePick.classList.contains('folded'), true); });
      // A click in the question box folds the list (not saved: Show list opens it again).
      var q = document.getElementById('question');
      if (q) q.addEventListener('pointerdown', function () { if (!scopePick.classList.contains('folded')) setFold(true, false); });
    }
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

  // Clipboard: navigator.clipboard needs https or localhost; on a plain http LAN address
  // fall back to a hidden textarea and execCommand('copy').
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var t = document.createElement('textarea');
      t.value = text; t.setAttribute('readonly', ''); t.style.position = 'fixed'; t.style.opacity = '0';
      document.body.appendChild(t); t.select();
      try { document.execCommand('copy') ? resolve() : reject(new Error('copy failed')); } catch (e) { reject(e); }
      t.remove();
    });
  }
  function flash(btn, label) {
    btn.textContent = 'Copied';
    setTimeout(function () { btn.textContent = label; }, 1500);
  }

  // A Copy button on every code block of an answer.
  function addCodeCopy(target) {
    target.querySelectorAll('pre').forEach(function (pre) {
      if (pre.querySelector('.code-copy')) return;
      var code = pre.querySelector('code') || pre;
      var b = el('button', 'code-copy', 'Copy');
      b.type = 'button';
      b.setAttribute('aria-label', 'Copy this code');
      b.addEventListener('click', function () {
        copyText(code.textContent).then(function () { flash(b, 'Copy'); }, function () { b.textContent = 'Copy failed'; });
      });
      pre.appendChild(b);
    });
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
    addCodeCopy(target);
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
      if (on) wrap.scrollIntoView({ block: 'start', behavior: scrollMode() });
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
    copy.onclick = function () { copyText(s.sql).then(function () { flash(copy, 'Copy SQL'); }, function () {}); };
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
      if (s.reserved) {
        var rb = el('span', 'badge', 'reserved');
        rb.title = 'Balanced retrieval kept this slot for its source (Sections per source)';
        meta.appendChild(rb);
      }
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
    var rt = st.retrieval;
    if (rt && rt.reserved) {
      // Balanced retrieval note: sections kept for sources the shared ranking left short.
      var by = Object.keys(rt.sources || {}).map(function (k) { return k + ' ' + rt.sources[k]; });
      parts.push(rt.reserved + (rt.reserved === 1 ? ' section' : ' sections') + ' reserved (' + by.join(', ') + ')');
    }
    var web = st.web;
    if (web) {
      // Web calls this answer made (pages reused or refused cost nothing) and their estimated cost.
      var names = { web_fetch: ['web fetch', 'web fetches'], local_fetch: ['local fetch', 'local fetches'], web_search: ['web search', 'web searches'], brave_search: ['Brave search', 'Brave searches'] };
      var bits = Object.keys(web.by_tool || {}).map(function (k) { var n = web.by_tool[k], w = names[k] || [k, k]; return n + ' ' + w[n === 1 ? 0 : 1]; });
      parts.push((bits.length ? bits.join(', ') : 'no paid web calls') + ' · $' + (web.cost < 0.01 && web.cost > 0 ? web.cost.toFixed(3) : web.cost.toFixed(2)) +
        (web.failed ? ' (' + web.failed + ' failed)' : ''));
    }
    container.appendChild(el('span', null, parts.join(' · ')));
    if (web && web.budget) {
      var b = web.budget, warn = el('span', 'stats-budget' + (b.stopped ? ' stopped' : ''),
        b.stopped ? 'Monthly web budget used up: no web calls until next month' : 'Monthly web budget ' + Math.round(b.used * 100) + ' % used');
      warn.title = '$' + b.spent.toFixed(2) + ' of $' + b.limit.toFixed(2) + ' (Settings → Web)';
      container.appendChild(warn);
    }
    var copy = el('button', 'link-btn', 'Copy answer');
    copy.type = 'button';
    copy.onclick = function () { copyText(m.content || '').then(function () { flash(copy, 'Copy answer'); }, function () {}); };
    container.appendChild(copy);
  }

  // This chat's questions, oldest first, for Up / Down arrow recall in the box.
  var sentQs = [], histPos = -1, histDraft = '';
  function addUser(text) {
    if (text && sentQs[sentQs.length - 1] !== text) sentQs.push(text);
    histPos = -1;
    var row = el('div', 'msg user');
    row.appendChild(el('div', 'bubble', text));
    // "Save prompt": a new saved prompt from this question and the sources picked now.
    var save = el('button', 'link-btn save-prompt', 'Save prompt');
    save.type = 'button';
    save.title = 'Keep this question with the sources picked below, to ask it again with one click (Saved menu)';
    save.onclick = function () {
      try { sessionStorage.setItem('ask.saved.draft', JSON.stringify({ prompt: text, scope: scopeValue() })); } catch (e) { /* private window */ }
      location.href = '/saved?new=1&from=chat';
    };
    row.appendChild(save);
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
  // "Jump to latest": shown while the reader is scrolled up and there is more below.
  var jump = el('button', 'jump-bottom', 'Jump to latest');
  jump.type = 'button'; jump.hidden = true;
  jump.setAttribute('aria-label', 'Jump to the latest message');
  jump.addEventListener('click', function () { scrollDown(true); jump.hidden = true; });
  var composerBox = document.querySelector('.composer');
  if (composerBox) composerBox.appendChild(jump);
  function updateJump() {
    var below = document.body.scrollHeight - (window.innerHeight + window.scrollY);
    jump.hidden = follow || below < 200;
  }
  window.addEventListener('scroll', function () {
    if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 80) follow = true;
    updateJump();
  }, { passive: true });
  function scrollDown(force) {
    if (force) follow = true;
    if (follow) window.scrollTo(0, document.body.scrollHeight);
    updateJump();
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

  // Saved prompts: a run may use its own model (for this chat only) and counts as a use.
  var run = { model: '', savedId: 0, title: '' };
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
    // Streaming render: markdown is parsed again over the whole answer, so do it at most
    // every 100 ms (plus a last time on done), and scroll in the same frame as the render.
    var lastPaint = 0, timer = 0;
    function cancelPaint() {
      pending = false;
      if (timer) { clearTimeout(timer); timer = 0; }
    }
    function paint() {
      cancelPaint();
      lastPaint = performance.now();
      renderMarkdown(a.text, content || '', a.prefix);
      if (thinking) a.thinkPre.textContent = thinking;
      scrollDown();
    }
    function schedulePaint() {
      if (pending) return;
      pending = true;
      timer = setTimeout(function () {
        timer = 0;
        requestAnimationFrame(function () { if (pending) paint(); });
      }, Math.max(0, 100 - (performance.now() - lastPaint)));
    }
    try {
      var res = await fetch('/ui/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
        body: JSON.stringify({ chat_id: chatId, question: question, scope: scopeValue(), model: run.model || undefined,
                               saved_id: run.savedId || undefined, title: (!chatId && run.title) || undefined })
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
              var t = (run.title || question).slice(0, 120);
              document.getElementById('chat-title').textContent = t;
              document.getElementById('delete-chat').hidden = false;
              addSidebarChat(chatId, t);
            }
            sourceCards(a.sources, payload.sources, a.prefix, a.strip);
            a.text.innerHTML = '<span class="typing"><i></i><i></i><i></i></span> <span class="subtle">' +
              (payload.tools ? (payload.sources.length ? 'Found ' + payload.sources.length + ' sources and ' : '') + payload.tools + ' tools, asking ' :
               payload.sources.length && payload.sources[0].kind === 'data' ? 'Query returned ' + payload.sources[0].row_count + ' rows, asking ' :
               payload.sources.length && payload.sources[0].kind === 'catalog' ? 'Catalog of ' + payload.sources.filter(function (s) { return s.kind === 'catalog'; })
                 .reduce(function (t, s) { return t + s.posts; }, 0) + ' posts, asking ' :
               payload.plain ? 'No sources, asking ' :
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
                if (go) setTimeout(function () { go.scrollIntoView({ behavior: scrollMode(), block: 'center' }); }, 50);
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
            schedulePaint();
          } else if (ev === 'token') {
            content += payload;
            schedulePaint();
          } else if (ev === 'done') {
            paint();
            statsLine(a.stats, { model: payload.model, stats: payload.stats, content: content });
          } else if (ev === 'error') {
            cancelPaint();
            if (payload.chat_id && !chatId) chatId = payload.chat_id;
            a.row.classList.add('failed');
            a.text.innerHTML = '';
            a.text.appendChild(el('div', 'alert alert-danger', payload.message));
          }
        }
        if (!pending) scrollDown();   // a pending render scrolls when it paints
      }
    } catch (e) {
      if (e.name === 'AbortError') {
        paint();
        a.stats.textContent = 'Stopped';
      } else {
        cancelPaint();
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
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); return; }
    // Up arrow in an empty box puts this chat's last question back in it to change and send
    // again; more Up presses go further back, Down comes forward (past the newest gives back
    // what was typed). Typing in a recalled question stops browsing, so the arrows then move
    // the cursor as usual. In a question of several lines, Up goes back only from the first
    // line and Down forward only from the last.
    if ((e.key !== 'ArrowUp' && e.key !== 'ArrowDown') || e.shiftKey || e.altKey || e.metaKey || e.ctrlKey || e.isComposing || !sentQs.length) return;
    var v = input.value, up = e.key === 'ArrowUp';
    if (input.selectionStart !== input.selectionEnd) return;
    var edge = up ? v.lastIndexOf('\n', input.selectionStart - 1) < 0 : v.indexOf('\n', input.selectionEnd) < 0;
    if (!edge) return;
    if (histPos < 0 && (!up || v.trim())) return;   // not browsing: only Up from an empty box starts
    e.preventDefault();
    if (histPos < 0) { histDraft = v; histPos = sentQs.length; }
    histPos += up ? -1 : 1;
    if (histPos < 0) histPos = 0;
    if (histPos >= sentQs.length) { histPos = -1; input.value = histDraft; }
    else input.value = sentQs[histPos];
    autosize();
    input.setSelectionRange(input.value.length, input.value.length);
  });
  function autosize() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 220) + 'px'; }
  input.addEventListener('input', function () { histPos = -1; autosize(); });
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
      card.scrollIntoView({ behavior: scrollMode(), block: 'center' });
      card.classList.add('flash'); setTimeout(function () { card.classList.remove('flash'); }, 1200);
    }
  });
  document.getElementById('delete-chat').addEventListener('click', async function () {
    if (!chatId || !(await vcoConfirm('Delete this chat?', { ok: 'Delete', danger: true }))) return;
    await fetch('/ui/chats/' + chatId + '/delete', { method: 'POST' });
    location.href = '/chat';
  });

  // -- Saved prompts: the Saved menu under the prompt, and /chat?saved=<id>[&edit=1] ------
  var WEB_NAMES = { 'web:fetch': 'Web fetch (Ollama)', 'web:local': 'Web fetch (local)', 'web:ollama': 'Web search (Ollama)', 'web:brave': 'Web search (Brave)' };
  function scopeWarn(text) {
    // A note under the Sources heading; it goes away when the sources are changed.
    if (!scopePick) return;
    var n = document.getElementById('scope-warn');
    if (!text) { if (n) n.remove(); return; }
    if (!n) {
      n = el('div', 'scope-warn'); n.id = 'scope-warn'; n.setAttribute('role', 'status');
      var head = scopePick.querySelector('.scope-head');
      head.parentNode.insertBefore(n, head.nextSibling);
    }
    n.textContent = text;
  }
  if (scopePick) scopePick.addEventListener('change', function () { scopeWarn(''); });
  function useSaved(item, edit) {
    // Tick the saved prompt's sources (not remembered as the last pick). A source with no button
    // now (deleted or turned off, web search without its key, MCP server down) is left out; when
    // none is left, the prompt runs with No sources (just the model), never All sources.
    var want = item.scope && item.scope !== 'all' ? item.scope.split(',') : [];
    var have = scopeBoxes.map(function (b) { return b.value; });
    var missing = want.filter(function (v) { return v !== 'none' && have.indexOf(v) < 0; });
    var keep = want.filter(function (v) { return have.indexOf(v) >= 0; });
    if (want.length && !keep.length) keep = ['none'];
    scopeBoxes.forEach(function (b) { b.checked = keep.indexOf(b.value) >= 0; });
    scopeRefresh(false);
    scopeWarn(missing.length ? 'Not available now: ' + missing.map(function (v) {
      return WEB_NAMES[v] || (v.indexOf('mcp:') === 0 ? v.slice(4) + ' (MCP)' : v);
    }).join(', ') + '. ' + (keep.length === 1 && keep[0] === 'none' ? 'Running with No sources (just the model).' : 'Running with the rest.') : '');
    run = { model: item.model || '', savedId: item.id, title: item.name || '' };
    var cur = document.querySelector('#model-menu .model-current');
    if (cur && item.model) { cur.textContent = item.model; cur.title = 'Model of the saved prompt "' + item.name + '", for this chat only'; }
    if (edit) { input.value = item.question; autosize(); input.focus(); return; }
    ask(item.question);
  }
  function askValues(item) {
    // A saved prompt with {variables}: ask for their values first (the last values used for this
    // prompt are kept in this browser, else the defaults). Resolves to the values, or null.
    return new Promise(function (resolve) {
      var key = 'ask.saved.vars.' + item.id, last = {};
      try { last = JSON.parse(localStorage.getItem(key) || '{}') || {}; } catch (e) { /* private window */ }
      var dlg = el('dialog', 'vars-dialog'), form = el('form', 'vars-form');
      form.method = 'dialog';
      form.appendChild(el('h2', null, item.name));
      form.appendChild(el('p', 'subtle', 'This saved prompt has values to fill in.'));
      item.variables.forEach(function (v, i) {
        var lab = el('label', 'vars-field'), inp = el('input');
        lab.appendChild(el('span', null, v.name));
        inp.name = v.name; inp.type = 'text'; inp.autocomplete = 'off';
        inp.value = last[v.name] != null ? last[v.name] : v.default || '';
        inp.placeholder = v.default ? 'default: ' + v.default : 'required';
        if (!v.default) inp.required = true;
        if (i === 0) inp.autofocus = true;
        lab.appendChild(inp);
        form.appendChild(lab);
      });
      var bar = el('div', 'vars-actions'), ok = el('button', 'btn btn-primary', 'Run'), cancel = el('button', 'btn btn-outline', 'Cancel');
      ok.type = 'submit'; ok.value = 'ok'; cancel.type = 'button';
      bar.appendChild(ok); bar.appendChild(cancel); form.appendChild(bar);
      dlg.appendChild(form); document.body.appendChild(dlg);
      var done = false;
      function finish(values) { if (done) return; done = true; dlg.close(); dlg.remove(); resolve(values); }
      cancel.addEventListener('click', function () { finish(null); });
      dlg.addEventListener('cancel', function () { finish(null); });
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        var values = {};
        item.variables.forEach(function (v) { values[v.name] = form.elements[v.name].value.trim(); });
        try { localStorage.setItem(key, JSON.stringify(values)); } catch (err) { /* private window */ }
        finish(values);
      });
      dlg.showModal();
    });
  }
  function openSaved(id, edit) {
    // A chat with messages starts a new chat; an empty one runs here.
    if (chatId || controller) { location.href = '/chat?saved=' + id + (edit ? '&edit=1' : ''); return; }
    fetch('/ui/saved/' + id, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error(r.status === 404 ? 'That saved prompt was deleted.' : r.statusText);
      return r.json();
    }).then(function (item) {
      if (!(item.variables || []).length) { useSaved(item, edit); return; }
      return askValues(item).then(function (values) {
        if (!values) return;
        return fetch('/ui/saved/' + id + '/question', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                         body: JSON.stringify({ values: values }) })
          .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || r.statusText); return d; }); })
          .then(function (d) { item.question = d.question; useSaved(item, edit); });
      });
    }).catch(function (e) { vcoAlert(e.message); });
  }
  var WEB_LABELS = { 'web:fetch': 'Web fetch (Ollama)', 'web:local': 'Web fetch (local)', 'web:ollama': 'Web search (Ollama)', 'web:brave': 'Web search (Brave)' };
  function missingText(list) {
    return list.map(function (v) { return WEB_LABELS[v] || (v.indexOf('mcp:') === 0 ? v.slice(4) + ' (MCP)' : v); }).join(', ');
  }
  var savedMenu = document.getElementById('saved-menu');
  if (savedMenu) {
    var savedList = savedMenu.querySelector('.model-list');
    // Search box and tag chips at the top of the menu; the tag pick is shared with the Saved prompts page.
    var menuTag = '';
    try { menuTag = localStorage.getItem('ask.saved.tag') || ''; } catch (e) { /* private window */ }
    savedMenu.addEventListener('toggle', async function () {
      if (!savedMenu.open) return;
      savedList.innerHTML = '';
      savedList.appendChild(el('div', 'model-msg', 'Loading…'));
      try {
        var d = await (await fetch('/ui/saved', { cache: 'no-store' })).json();
        savedList.innerHTML = '';
        if (!d.items.length) savedList.appendChild(el('div', 'model-msg', 'No saved prompts yet. Use Save prompt under a question, or add one on the Saved prompts page.'));
        var tags = [];
        d.items.forEach(function (it) { (it.tag_list || []).forEach(function (t) { if (tags.indexOf(t) < 0) tags.push(t); }); });
        tags.sort();
        if (tags.indexOf(menuTag) < 0) menuTag = '';
        var rowsEl = [], search = null, noMatch = el('div', 'model-msg', 'No saved prompt matches.');
        function filterMenu() {
          var words = (search ? search.value : '').toLowerCase().split(/\s+/).filter(Boolean), shown = 0;
          rowsEl.forEach(function (r) {
            var ok = (!menuTag || r.tags.indexOf(menuTag) >= 0) && words.every(function (w) { return r.text.indexOf(w) >= 0; });
            r.el.hidden = !ok; if (ok) shown++;
          });
          noMatch.hidden = shown > 0;
        }
        if (d.items.length) {
          var head = el('div', 'saved-head');
          search = el('input', 'saved-search');
          search.type = 'search'; search.placeholder = 'Search saved prompts'; search.setAttribute('aria-label', 'Search saved prompts');
          search.addEventListener('input', filterMenu);
          search.addEventListener('keydown', function (e) {
            if (e.key !== 'Enter') return;
            e.preventDefault();
            var first = rowsEl.filter(function (r) { return !r.el.hidden; })[0];
            if (first) { savedMenu.open = false; openSaved(first.id, e.shiftKey); }
          });
          head.appendChild(search);
          if (tags.length) {
            var chips = el('div', 'saved-tags');
            [''].concat(tags).forEach(function (t) {
              var c = el('button', 'tag-chip' + (t === menuTag ? ' on' : ''), t || 'All');
              c.type = 'button'; c.setAttribute('aria-pressed', t === menuTag ? 'true' : 'false');
              c.addEventListener('click', function () {
                menuTag = t;
                try { localStorage.setItem('ask.saved.tag', t); } catch (e) { /* private window */ }
                chips.querySelectorAll('.tag-chip').forEach(function (x) { var on = x === c; x.classList.toggle('on', on); x.setAttribute('aria-pressed', on ? 'true' : 'false'); });
                filterMenu(); search.focus();
              });
              chips.appendChild(c);
            });
            head.appendChild(chips);
          }
          savedList.appendChild(head);
        }
        d.items.forEach(function (it) {
          var row = el('div', 'saved-item');
          var b = el('button', 'model-item');
          b.type = 'button'; b.setAttribute('role', 'menuitem');
          b.title = 'Run in a new chat: ' + it.prompt.slice(0, 200);
          b.appendChild(el('span', 'model-name', it.name));
          var names = {};
          scopeBoxes.forEach(function (x) { names[x.value] = x.parentNode.textContent.trim(); });
          var info = [it.scope === 'all' ? 'All sources' : it.scope.split(',').map(function (v) { return names[v] || v; }).join(', ')];
          if (it.model) info.push(it.model);
          if ((it.tag_list || []).length) info.push('#' + it.tag_list.join(' #'));
          b.appendChild(el('span', 'model-tags', info.join(' · ')));
          if ((it.unavailable || []).length) {
            var w = el('span', 'saved-warn', 'Not available now: ' + missingText(it.unavailable));
            w.title = 'Run leaves these out' + (it.unavailable.length === it.scope.split(',').length ? ' and answers with No sources (just the model)' : '');
            b.appendChild(w);
          }
          if ((it.variables || []).length) b.appendChild(el('span', 'model-tags', 'Asks for: ' + it.variables.map(function (v) { return v.name; }).join(', ')));
          b.addEventListener('click', function () { savedMenu.open = false; openSaved(it.id, false); });
          var pin = el('button', 'saved-pin' + (it.pinned ? ' on' : ''), it.pinned ? '★' : '☆');
          pin.type = 'button';
          pin.title = it.pinned ? 'Unpin' : 'Pin to the top';
          pin.setAttribute('aria-label', (it.pinned ? 'Unpin ' : 'Pin ') + it.name);
          pin.setAttribute('aria-pressed', it.pinned ? 'true' : 'false');
          pin.addEventListener('click', function (e) {
            e.stopPropagation();
            fetch('/ui/saved/' + it.id + '/pin', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on: !it.pinned }) })
              .then(function (r) { if (!r.ok) throw new Error(r.statusText); savedMenu.dispatchEvent(new Event('toggle')); })
              .catch(function (err) { vcoAlert(err.message); });
          });
          var e2 = el('button', 'saved-edit', 'Edit first');
          e2.type = 'button'; e2.title = 'Put it in the box with its sources picked, to change it before sending';
          e2.addEventListener('click', function () { savedMenu.open = false; openSaved(it.id, true); });
          row.appendChild(pin); row.appendChild(b); row.appendChild(e2);
          savedList.appendChild(row);
          rowsEl.push({ el: row, id: it.id, tags: it.tag_list || [],
                        text: [it.name, it.prompt, it.urls, it.tags, it.scope].join(' ').toLowerCase() });
        });
        if (d.items.length) { noMatch.hidden = true; savedList.appendChild(noMatch); filterMenu(); }
        savedList.appendChild(el('a', 'model-foot', 'Manage saved prompts')).href = '/saved';
        if (search) search.focus();
      } catch (e) { savedList.innerHTML = ''; savedList.appendChild(el('div', 'model-msg err', e.message)); }
    });
    document.addEventListener('click', function (e) { if (savedMenu.open && !savedMenu.contains(e.target)) savedMenu.open = false; });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && savedMenu.open) { savedMenu.open = false; savedMenu.querySelector('summary').focus(); } });
  }
  // Picking a model in the model menu ends a saved prompt's own model.
  var modelBox = document.getElementById('model-menu');
  if (modelBox) modelBox.addEventListener('click', function (e) { if (e.target.closest('.model-item')) run.model = ''; });

  (window.ASK.messages || []).forEach(renderSaved);
  if (window.ASK.messages && window.ASK.messages.length) scrollDown();
  input.focus();
  var qs = new URLSearchParams(location.search);
  if (qs.get('saved') && !chatId) openSaved(qs.get('saved'), qs.get('edit') === '1');
})();
