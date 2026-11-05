/* HTML report page: summary (marked + DOMPurify), charts (ECharts via AskCharts), sortable and
   filterable tables, light/dark switch and print. Inlined into each report file. */
(function () {
  var data = JSON.parse(document.getElementById('r-data').textContent);
  var charts = [];

  var sum = document.getElementById('r-summary');
  if (sum && data.summary) sum.innerHTML = DOMPurify.sanitize(marked.parse(data.summary));

  function drawAll() {
    charts.forEach(function (c) { c.chart.setOption(AskCharts.option(c.spec, false, false), true); });
  }
  document.querySelectorAll('[data-chart]').forEach(function (box) {
    var spec = data.panels[+box.getAttribute('data-chart')].chart;
    var chart = echarts.init(box, null, { renderer: 'canvas' });
    chart.setOption(AskCharts.option(spec, false, false));
    charts.push({ chart: chart, spec: spec });
    if ('ResizeObserver' in window) new ResizeObserver(function () { chart.resize(); }).observe(box);
  });
  window.addEventListener('resize', function () { charts.forEach(function (c) { c.chart.resize(); }); });
  window.addEventListener('beforeprint', function () { charts.forEach(function (c) { c.chart.resize(); }); });

  function fmt(v) {
    if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString() : (Math.round(v * 100) / 100).toLocaleString();
    return v == null ? '' : String(v);
  }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  document.querySelectorAll('[data-table]').forEach(function (holder) {
    var p = data.panels[+holder.getAttribute('data-table')];
    var cols = p.columns || [], rows = (p.rows || []).slice();
    var numeric = cols.map(function (_, i) {
      var vals = rows.map(function (r) { return r[i]; }).filter(function (v) { return v != null && v !== ''; });
      return vals.length > 0 && vals.every(function (v) { return typeof v === 'number'; });
    });
    var filter = null;
    if (holder.getAttribute('data-filter') && rows.length > 8) {
      filter = el('input', 'r-filter'); filter.type = 'search'; filter.placeholder = 'Filter rows'; filter.setAttribute('aria-label', 'Filter rows');
      holder.appendChild(filter);
    }
    var wrap = el('div', 'r-table-wrap'), table = el('table', 'r-table'), thead = el('thead'), tbody = el('tbody'), htr = el('tr');
    var count = el('div', 'r-count');
    var sortCol = -1, desc = false;
    cols.forEach(function (c, i) {
      var th = el('th', numeric[i] ? 'num' : null, c);
      th.title = 'Sort by ' + c;
      th.onclick = function () {
        desc = sortCol === i ? !desc : numeric[i];
        sortCol = i;
        htr.querySelectorAll('th').forEach(function (h) { h.classList.remove('sorted', 'desc'); });
        th.classList.add('sorted'); if (desc) th.classList.add('desc');
        rows.sort(function (a, b) {
          var x = a[i], y = b[i];
          if (x == null) return 1; if (y == null) return -1;
          var r = numeric[i] ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true, sensitivity: 'base' });
          return desc ? -r : r;
        });
        render();
      };
      htr.appendChild(th);
    });
    thead.appendChild(htr); table.appendChild(thead); table.appendChild(tbody); wrap.appendChild(table);
    holder.appendChild(wrap); holder.appendChild(count);
    function render() {
      var q = filter ? filter.value.trim().toLowerCase() : '';
      tbody.innerHTML = '';
      var shown = 0;
      rows.forEach(function (r) {
        if (q && !r.some(function (v) { return v != null && String(v).toLowerCase().indexOf(q) >= 0; })) return;
        var tr = el('tr');
        r.forEach(function (v, i) { tr.appendChild(el('td', numeric[i] ? 'num' : null, fmt(v))); });
        tbody.appendChild(tr); shown++;
      });
      count.textContent = (q ? shown + ' of ' : '') + rows.length + ' row' + (rows.length === 1 ? '' : 's') + (p.truncated ? ' (query limit reached)' : '');
    }
    if (filter) filter.addEventListener('input', render);
    render();
  });

  document.getElementById('r-theme').onclick = function () {
    var r = document.documentElement, t = r.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    r.setAttribute('data-theme', t);
    try { localStorage.setItem('vco-theme', t); } catch (e) {}
    drawAll();
  };
  document.getElementById('r-print').onclick = function () { window.print(); };
})();
