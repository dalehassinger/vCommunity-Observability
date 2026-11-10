/* Chart options for Apache ECharts, shared by the chat (ask.js) and the HTML reports.
   A chart is a spec built on the server from a query result:
   {type: pie|donut|bar|line, title, label, series: [names], categories: [...], values: [[...], ...]}.
   Colors and text follow the page's theme variables (light/dark). */
(function () {
  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }
  var PALETTE = ['#0072a3', '#3c8500', '#c27b00', '#8939ad', '#00a1b3', '#db2100', '#6870c4', '#5d8a00', '#d16a00', '#a6306b', '#4a90a4', '#8a9aa3'];
  var PALETTE_DARK = ['#49afd9', '#5eb715', '#f5c24b', '#c58ae0', '#2fc7d6', '#ff6b52', '#8f96e8', '#9bd14a', '#ff9f43', '#e070a6', '#7cc3d6', '#a8b6bd'];
  // big = the enlarged (full-window) view: more room, so longer labels and smaller slices labelled.
  function chartOption(c, big, showTitle) {
    var dark = document.documentElement.getAttribute('data-theme') === 'dark';
    var text = cssVar('--text', '#21333b'), subtle = cssVar('--subtle', '#5d6c74'), border = cssVar('--border', '#dde3e7');
    var base = { color: dark ? PALETTE_DARK : PALETTE, backgroundColor: 'transparent', animationDuration: 400,
      animation: !(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches),
      textStyle: { color: text, fontFamily: getComputedStyle(document.body).fontFamily },
      tooltip: { confine: true }, title: { show: showTitle === undefined ? !big : showTitle, text: c.title, left: 'center', top: 0, textStyle: { color: text, fontSize: 13, fontWeight: 600 } } };
    if (c.type === 'pie' || c.type === 'donut') {
      base.tooltip.trigger = 'item';
      base.tooltip.formatter = '{b}: {c} ({d}%)';
      // Legend along the bottom, so slice labels have both sides to themselves (enlarged: the
      // whole legend, wrapped, and labels lined up at the edges so none is cut off). Small slices get no label (legend and tooltip name them),
      // long names are cut with "..." and any labels that would still collide are hidden.
      var total = c.values.reduce(function (t, v) { return t + (v[0] || 0); }, 0) || 1;
      var minPct = big ? 1 : 3;
      base.legend = big
        ? { type: 'plain', orient: 'horizontal', left: 16, right: 16, bottom: 0, itemGap: 14, textStyle: { color: subtle, width: 300, overflow: 'truncate' }, tooltip: { show: true } }
        : { type: 'scroll', orient: 'horizontal', left: 8, right: 8, bottom: 0, textStyle: { color: subtle, width: 190, overflow: 'truncate' }, tooltip: { show: true } };
      base.series = [{ type: 'pie', name: c.series[0],
        radius: c.type === 'donut' ? (big ? ['36%', '62%'] : ['34%', '58%']) : (big ? ['0%', '62%'] : ['0%', '58%']),
        center: big ? ['50%', '46%'] : ['50%', '50%'], avoidLabelOverlap: true,
        itemStyle: { borderColor: cssVar('--surface', '#fff'), borderWidth: 2 },
        label: { color: text, formatter: '{b}\n{d}%', width: big ? 240 : 150, overflow: 'truncate', lineHeight: 15,
          alignTo: big ? 'edge' : 'none', edgeDistance: 16 },
        labelLine: { lineStyle: { color: border }, length: 10, length2: 12 },
        labelLayout: { hideOverlap: true },
        data: c.categories.map(function (name, i) {
          var v = c.values[i][0];
          return { name: name, value: v, label: { show: v / total * 100 >= minPct }, labelLine: { show: v / total * 100 >= minPct } };
        }) }];
      return base;
    }
    var many = c.categories.length > 8;
    base.tooltip.trigger = 'axis';
    base.legend = c.series.length > 1 ? { top: (showTitle === undefined ? !big : showTitle) ? 22 : 0, textStyle: { color: subtle } } : undefined;
    base.grid = { left: 8, right: big ? 16 : 44, top: (showTitle === undefined ? !big : showTitle) ? (c.series.length > 1 ? 52 : 34) : (c.series.length > 1 ? 30 : 8), bottom: 8, containLabel: true };
    base.xAxis = { type: 'category', data: c.categories, axisLabel: { color: subtle, rotate: many ? 40 : 0, interval: 0, hideOverlap: true },
      axisLine: { lineStyle: { color: border } } };
    base.yAxis = { type: 'value', axisLabel: { color: subtle }, splitLine: { lineStyle: { color: border } } };
    base.series = c.series.map(function (name, j) {
      return { type: c.type === 'line' ? 'line' : 'bar', name: name, smooth: false, barMaxWidth: 48,
        data: c.values.map(function (v) { return v[j]; }) };
    });
    return base;
  }

  window.AskCharts = { cssVar: cssVar, option: chartOption, PALETTE: PALETTE, PALETTE_DARK: PALETTE_DARK };
})();
