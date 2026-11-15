/* Apple HomeView display: Dale's HomeView design (1088 x 1088 canvas scaled to fit the screen,
   20-second auto-cycle) filled with live lab data from GET /ui/homeview/data.

   Sections (the menu under the home icon, top left): Home, Lab, Hosts, VMs, Storage, Clusters, Health, GitHub (order and hidden pages set on the Settings page). A section
   whose module is off or has no data is left out. A list too long for one screen is split
   into several pages of the same section (Hosts 1/2, 2/2 ...); Auto-cycle and the arrow keys
   go through every page. Links: #home, #lab, #hosts, #hosts-2 ... */
(function () {
  'use strict';
  var OWNER = 'Dale';
  var TIMEZONE = 'America/New_York';
  var CYCLE_MS = 20000;       // default 20 s like the original HomeView; Settings page sets it (cycle_seconds)
  var REFRESH_MS = 60000;     // reload the data every minute
  // Rows per page: the full 1088 x 1088 canvas, and the short, wide Safari screen where
  // homeview-base.css stops scaling (same media query), which has room for fewer rows.
  var FULL = { clusters: 7, hosts: 9, findings: 12, datastores: 10, vms: 12 };
  var SHORT = { clusters: 5, hosts: 7, findings: 7, datastores: 7, vms: 7 };
  var shortQuery = window.matchMedia('(min-width: 360px) and (max-height: 520px) and (min-aspect-ratio: 11/10)');
  var PER = shortQuery.matches ? SHORT : FULL;
  // Show menu (header, list pages only): 'fit' = rows that fit the screen, no scrolling (default);
  // 10 / 25 / 50 / 100 = that many rows per page, the list scrolls. Remembered per section on this screen.
  var SHOW_CHOICES = [10, 25, 50, 100];
  var LISTS = {   // section -> [PER key, function giving the full list]
    clusters: ['clusters', function (d) { return d.dbh.clusters || []; }],
    hosts: ['hosts', function (d) { return sortedList('hosts', d); }],
    vms: ['vms', function (d) { return sortedList('vms', d); }],
    health: ['findings', function (d) { return d.dbh.findings || []; }],
    storage: ['datastores', function (d) { return sortedList('storage', d); }]
  };
  // Sort menus (the green tag in the list header) on VMs, Hosts and Storage; the first choice is the
  // default. Remembered per section on this screen (localStorage homeview.sort).
  var SORTS = {
    vms: [['cpu', 'BY CPU'], ['mem', 'BY MEMORY']],
    hosts: [['name', 'BY NAME'], ['cpu', 'BY CPU'], ['mem', 'BY MEMORY']],
    storage: [['used', 'BY USED'], ['runway', 'BY RUNWAY'], ['size', 'BY SIZE'], ['name', 'BY NAME']]
  };
  var sortBy = {};
  try {
    sortBy = JSON.parse(localStorage.getItem('homeview.sort') || '{}') || {};
    if (!sortBy.vms && localStorage.getItem('homeview.vmsort') === 'mem') sortBy.vms = 'mem';   // earlier VMs-only setting
  } catch (e) { sortBy = {}; }
  function sortKey(section) {
    var ok = SORTS[section].some(function (o) { return o[0] === sortBy[section]; });
    return ok ? sortBy[section] : SORTS[section][0][0];
  }
  function desc(field) { return function (a, b) { return (Number(b[field]) || 0) - (Number(a[field]) || 0); }; }
  function byName(a, b) { return String(a.name).localeCompare(String(b.name), undefined, { numeric: true }); }
  function sortedList(section, d) {
    var k = sortKey(section);
    if (section === 'vms') return (k === 'mem' ? d.dbh.busiest_vms_mem : d.dbh.busiest_vms) || [];
    if (section === 'hosts') {
      var h = (d.dbh.hosts || []).slice();          // server order: cluster, then name
      return k === 'cpu' ? h.sort(desc('cpu_pct')) : k === 'mem' ? h.sort(desc('mem_pct')) : h;
    }
    var s = ((d.storage && d.storage.items) || []).slice();   // server order: most used first
    if (k === 'runway') return s.sort(function (a, b) {
      var x = a.runway_days == null ? Infinity : a.runway_days, y = b.runway_days == null ? Infinity : b.runway_days;
      return x - y || (b.used_pct || 0) - (a.used_pct || 0);
    });
    return k === 'size' ? s.sort(desc('capacity_tb')) : k === 'name' ? s.sort(byName) : s;
  }
  function sortMenu(section, what) {
    var k = sortKey(section);
    return '<label class="hv-sort" title="Sort the ' + what + '"><select data-sort="' + section + '" aria-label="Sort ' + what + ' by">' +
      SORTS[section].map(function (o) { return '<option value="' + o[0] + '"' + (o[0] === k ? ' selected' : '') + '>' + o[1] + '</option>'; }).join('') +
      '</select></label>';
  }
  var showRows = {};
  try { showRows = JSON.parse(localStorage.getItem('homeview.show') || '{}') || {}; } catch (e) { showRows = {}; }
  function per(section) { return SHOW_CHOICES.indexOf(showRows[section]) >= 0 ? showRows[section] : PER[LISTS[section][0]]; }
  function scrolls(section) { return SHOW_CHOICES.indexOf(showRows[section]) >= 0 ? ' hv-scroll' : ''; }
  // The Show menu sits in the list's own header row; only when the list is longer than what fits (or after a pick).
  function showPicker(section, d) {
    var total = LISTS[section][1](d).length, fit = PER[LISTS[section][0]], picked = SHOW_CHOICES.indexOf(showRows[section]) >= 0;
    if (total <= fit && !picked) return '';
    return '<label class="hv-show" title="Rows per page. Fit to screen never scrolls; more rows scroll inside the list.">Show <select data-show="' + section + '">' +
      '<option value="fit"' + (picked ? '' : ' selected') + '>Fit to screen (' + Math.min(fit, total) + ')</option>' +
      SHOW_CHOICES.map(function (n) { return '<option value="' + n + '"' + (showRows[section] === n ? ' selected' : '') + '>' + n + ' per page</option>'; }).join('') +
      '</select></label>';
  }

  var display = document.getElementById('display');
  var main = document.getElementById('pages');
  var navBox = document.getElementById('nav-pages');

  // -- screen size: the whole square canvas scales uniformly (unchanged from HomeView) ----
  function fitDisplay() {
    var v = window.visualViewport;
    var width = v ? v.width : window.innerWidth, height = v ? v.height : window.innerHeight;
    display.style.setProperty('--scale', Math.min(width / 1088, height / 1088));
  }
  window.addEventListener('resize', fitDisplay);
  if (window.visualViewport) window.visualViewport.addEventListener('resize', fitDisplay);
  fitDisplay();

  // -- helpers ------------------------------------------------------------------------
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function icon(id) { return '<svg><use href="#' + id + '"/></svg>'; }
  function num(v, d) { return v == null || isNaN(v) ? '–' : Number(v).toLocaleString('en-US', { maximumFractionDigits: d == null ? 0 : d }); }
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function pct(v) { return v == null ? '–' : num(v, v >= 10 ? 0 : 1); }
  function clamp(v) { return Math.max(0, Math.min(100, Number(v) || 0)); }
  // Colors follow VCF Operations: green under 75 %, amber to 90 %, red above.
  function level(v, warn, crit) { v = Number(v) || 0; return v >= (crit || 90) ? 'crit' : v >= (warn || 75) ? 'warn' : 'ok'; }
  function meter(v, warn, crit) {
    return '<div class="meter hv-meter ' + level(v, warn, crit) + '"><i style="width:' + clamp(v) + '%"></i></div>';
  }
  function chunk(list, n) { var out = []; for (var i = 0; i < list.length; i += n) out.push(list.slice(i, i + n)); return out.length ? out : [[]]; }
  function gib(v) { return v == null ? '–' : v >= 1024 ? num(v / 1024, 1) + ' TiB' : num(v) + ' GiB'; }
  function ago(ts) {
    if (!ts) return 'never';
    var t = Date.parse(String(ts).replace(' ', 'T') + (/[zZ+]/.test(String(ts).slice(10)) ? '' : 'Z'));
    if (isNaN(t)) return String(ts);
    var m = Math.round((Date.now() - t) / 60000);
    return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : m < 1440 ? Math.round(m / 60) + ' h ago' : Math.round(m / 1440) + ' d ago';
  }
  function shortDate(ts) {
    var t = Date.parse(String(ts || '').replace(' ', 'T'));
    return isNaN(t) ? '' : new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: TIMEZONE }).format(new Date(t));
  }
  function more(n) { return n > 0 ? '<span class="tag">+' + n + ' MORE</span>' : ''; }

  // -- page builders --------------------------------------------------------------------
  function heading(eyebrow, title, id) {
    return '<div class="page-heading"><div><p class="eyebrow"' + (id ? ' id="' + id + '-eyebrow"' : '') + '>' + esc(eyebrow) +
      '</p><h1' + (id ? ' id="' + id + '-title"' : '') + '>' + esc(title) + '</h1></div><span class="page-count">' +
      '<b class="pc-now">01</b> <span class="pc-all">/ 01</span></span></div>';
  }

  function attention(d) {
    var out = [], db = d.dbh, st = d.storage, gh = d.github;
    if (db.available) {
      var t = db.totals;
      if (t.hosts_connected < t.hosts) out.push(['red', (t.hosts - t.hosts_connected) + ' host' + (t.hosts - t.hosts_connected > 1 ? 's' : '') + ' not connected', 'Check vCenter']);
      if (t.hosts_maintenance) out.push(['gold', t.hosts_maintenance + ' host' + (t.hosts_maintenance > 1 ? 's' : '') + ' in maintenance mode', 'Workloads moved off']);
      if (t.findings) out.push(['gold', t.findings + ' health finding' + (t.findings > 1 ? 's' : ''), db.findings_by_type.slice(0, 3).map(function (f) { return f.type + ' ' + f.count; }).join(' · ')]);
      if (t.snapshots) out.push(['blue', t.snapshots + ' snapshot' + (t.snapshots > 1 ? 's' : '') + ' kept', 'Oldest from ' + shortDate((db.snapshots[0] || {}).date)]);
      if (t.tools_upgrade) out.push(['blue', t.tools_upgrade + ' VM' + (t.tools_upgrade > 1 ? 's' : '') + ' need a VMware Tools upgrade', 'Powered-on VMs']);
    }
    if (st.available && st.totals.over_80) out.push(['red', st.totals.over_80 + ' datastore' + (st.totals.over_80 > 1 ? 's' : '') + ' over 80 % used', 'Lowest free ' + pct(st.totals.lowest_free_pct) + ' %']);
    if (gh.available && gh.incidents.length) out.push(['gold', 'GitHub: ' + gh.incidents[0].name, gh.status.description]);
    if (!out.length) out.push(['mint', 'Nothing needs you right now.', 'All checks are quiet.']);
    return out;
  }

  // A small stack of three mini PCs (MS-01 style) on the clock card. Each unit's status light is
  // one of the lab's hosts (by name): green online, amber maintenance, red not connected, dark = no host.
  function miniStack(d) {
    var hosts = (d.dbh.available ? d.dbh.hosts || [] : []).slice().sort(byName).slice(0, 3);
    var W = 380, H = 58, GAP = 8, DX = 22, DY = 16, units = [];
    for (var i = 0; i < 3; i++) {
      var h = hosts[i], y = DY + i * (H + GAP);
      var st = !h ? 'off' : h.state !== 'connected' ? 'crit' : h.maintenance ? 'warn' : 'ok';
      var label = h ? esc(String(h.name).split('.')[0]) : '';
      var vents = '';
      for (var r = 0; r < 4; r++) for (var c = 0; c < 15; c++)
        vents += '<rect x="' + (22 + c * 11 + (r % 2) * 5) + '" y="' + (20 + r * 8) + '" width="7" height="3" rx="1.5"/>';
      units.push(
        '<g class="mp-unit mp-' + st + '" transform="translate(0 ' + y + ')">' +
        '<path class="mp-top" d="M0 0 L' + DX + ' ' + (-DY) + ' L' + (W + DX) + ' ' + (-DY) + ' L' + W + ' 0 Z"/>' +
        '<path class="mp-side" d="M' + W + ' 0 L' + (W + DX) + ' ' + (-DY) + ' L' + (W + DX) + ' ' + (H - DY) + ' L' + W + ' ' + H + ' Z"/>' +
        '<rect class="mp-front" width="' + W + '" height="' + H + '" rx="5"/>' +
        '<rect class="mp-bezel" x="4" y="4" width="' + (W - 8) + '" height="' + (H - 8) + '" rx="3"/>' +
        '<g class="mp-vents">' + vents + '</g>' +
        (label ? '<text class="mp-label" x="22" y="14">' + label + '</text>' : '') +
        '<rect class="mp-port" x="214" y="23" width="17" height="9" rx="1.5"/><rect class="mp-port" x="238" y="23" width="17" height="9" rx="1.5"/>' +
        '<rect class="mp-port" x="262" y="24" width="15" height="7" rx="3.5"/><rect class="mp-port" x="284" y="24" width="15" height="7" rx="3.5"/>' +
        '<circle class="mp-port" cx="312" cy="27.5" r="3.5"/>' +
        '<circle class="mp-btn" cx="345" cy="27.5" r="10"/><circle class="mp-led" cx="345" cy="27.5" r="5.5"/>' +
        '<rect class="mp-act" x="364" y="25" width="4" height="5" rx="1" style="animation-delay:' + (i * 0.7) + 's"/>' +
        '<rect class="mp-foot" x="24" y="' + H + '" width="30" height="3" rx="1"/><rect class="mp-foot" x="' + (W - 54) + '" y="' + H + '" width="30" height="3" rx="1"/>' +
        '</g>');
    }
    var vbH = DY + 3 * H + 2 * GAP + 4;
    return '<div class="hv-rack" aria-hidden="true"><svg viewBox="-2 -2 ' + (W + DX + 4) + ' ' + (vbH + 4) + '">' +
      '<defs><linearGradient id="mpFront" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3a444c"/><stop offset="1" stop-color="#262e34"/></linearGradient>' +
      '<linearGradient id="mpTop" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#46525b"/><stop offset="1" stop-color="#36414a"/></linearGradient>' +
      '<filter id="mpGlow" x="-2" y="-2" width="5" height="5"><feGaussianBlur stdDeviation="3"/></filter></defs>' +
      // drawn bottom unit first, so each unit sits on top of the one below
      units.reverse().join('') + '</svg></div>';
  }

  function homePage(d) {
    var db = d.dbh, t = db.available ? db.totals : null, vc = (db.vcenters || [])[0];
    var items = attention(d), shown = items.slice(0, 3);
    var att = shown.map(function (a) {
      return '<div class="agenda-row"><span class="event-dot-lg ' + a[0] + '"></span><div><h3>' + esc(a[1]) + '</h3><p>' + esc(a[2]) + '</p></div></div>';
    }).join('');
    return '<section class="page" data-section="home">' + heading('WELCOME HOME', 'Good morning, ' + OWNER + '.', 'home') +
      '<div class="home-grid">' +
      '<article class="clock-card card"><div class="card-top"><span class="eyebrow">MAKE TIME FOR TODAY</span></div>' +
      '<div class="time-row"><time class="hv-clock">--:--</time><span class="hv-period">AM</span></div><div class="clock-bottom"><span class="hv-weekday">Sunday</span></div><div class="clock-art" aria-hidden="true"></div>' + miniStack(d) + '</article>' +
      '<article class="card weather hv-vcenter"><div class="card-top"><h2>vCenter</h2><span class="tag live-tag">LIVE</span></div>' +
      (t ? '<div class="hv-vc-name">' + esc(vc ? vc.name : 'vCenter') + '</div><p class="hv-vc-ver">' + esc(vc ? 'vCenter ' + vc.version + ' · build ' + vc.build : '') + '</p>' +
        '<div class="hv-mini-stats"><div><strong>' + t.hosts_connected + '<small>/' + t.hosts + '</small></strong><span>Hosts connected</span></div>' +
        '<div><strong>' + t.vms_on + '<small>/' + t.vms + '</small></strong><span>VMs running</span></div></div>' +
        '<div class="weather-low">Collected ' + esc(ago(db.collected_at)) + '<span>' + (db.vcenters.length > 1 ? db.vcenters.length + ' vCenters' : '') + '</span></div>'
        : '<p class="hv-empty">' + esc(db.why || 'No data yet') + '</p>') + '</article>' +
      '<article class="card agenda hv-attention"><div class="card-top"><h2>Needs your attention</h2>' + more(items.length - shown.length) + '</div>' + att + '</article>' +
      '<article class="card home-summary"><div><span class="eyebrow">A GLANCE AT THE LAB</span><h2>' + (t ? (items[0][0] === 'mint' ? 'Everything looks good.' : 'A few things to look at.') : 'Waiting for data.') + '</h2><p>Live from your vCenter</p></div>' +
      '<div class="reading"><span>Host CPU in use</span><strong>' + (t ? pct(t.cpu_pct) : '–') + '<small>%</small></strong></div>' +
      '<div class="reading"><span>Host memory in use</span><strong>' + (t ? pct(t.mem_pct) : '–') + '<small>%</small></strong></div></article>' +
      '</div></section>';
  }

  function labPage(d) {
    var db = d.dbh, t = db.totals, issues = t.findings + (t.hosts - t.hosts_connected);
    var cl = db.clusters.slice(0, 2);
    var used = t.ds_used_pct || 0;
    return '<section class="page" data-section="lab">' + heading('YOUR SYSTEMS, AT A GLANCE', 'Home lab.') +
      '<div class="lab-grid">' +
      '<article class="card lab-banner"><div><span class="eyebrow">LIVE FROM DBH INSIGHTS</span><h2>' + (issues ? 'Running, with a few notes.' : 'Everything in its place.') + '</h2><p>' + esc((db.vcenters[0] || {}).name || '') + ' · collected ' + esc(ago(db.collected_at)) + '</p></div>' +
      '<div class="health-badge ' + (issues ? 'warn' : '') + '"><span class="dot"></span> ' + (issues ? issues + ' to review' : 'All systems healthy') + '</div>' +
      '<div class="banner-stats"><div><strong>' + pad2(t.hosts) + '</strong><span>ESX hosts</span></div><div><strong>' + pad2(t.vms_on) + '</strong><span>VMs running of ' + t.vms + '</span></div>' +
      '<div><strong>' + pad2(t.datastores) + '</strong><span>Datastores</span></div><div><strong>' + t.findings + '</strong><span>Health findings</span></div></div></article>' +
      '<article class="card resource-card"><div class="card-top"><h2>Compute</h2><span class="tag live-tag">LIVE</span></div>' +
      '<div class="metric-line"><span>CPU utilization</span><strong>' + pct(t.cpu_pct) + '<small>%</small></strong></div>' + meter(t.cpu_pct) +
      '<p class="metric-note">' + t.cores + ' cores · ' + t.vcpus_on + ' vCPUs running</p>' +
      '<div class="metric-line second"><span>Memory utilization</span><strong>' + pct(t.mem_pct) + '<small>%</small></strong></div>' + meter(t.mem_pct) +
      '<p class="metric-note">' + gib(t.dram_gib) + ' DRAM · ' + gib(t.vram_on_gib) + ' vRAM running</p></article>' +
      '<article class="card storage-card"><div class="card-top"><h2>Storage</h2><span class="tag live-tag">LIVE</span></div><div class="storage-body">' +
      '<div class="storage-ring ' + level(used) + '" style="--p:' + clamp(used) + '%"><div><strong>' + pct(used) + '<span>%</span></strong><span>used</span></div></div>' +
      '<div><h3>' + t.datastores + ' datastores</h3><p>' + gib(t.ds_used_gib) + ' used</p><p>' + gib(t.ds_capacity_gib - t.ds_used_gib) + ' available</p>' +
      '<span class="storage-state ' + level(used) + '"><span class="dot"></span> ' + (used >= 90 ? 'Nearly full' : used >= 75 ? 'Getting full' : 'Healthy') + '</span></div></div>' +
      '<div class="storage-footer"><span>' + gib(t.ds_capacity_gib) + ' total capacity</span><span>' + esc(Array.from(new Set(db.datastores.map(function (x) { return x.type; }))).join(' · ')) + '</span></div></article>' +
      '<article class="card systems"><div class="card-top"><h2>Clusters</h2>' + more(db.clusters.length - cl.length) + '</div>' +
      cl.map(function (c) {
        var ok = c.hosts_available === c.hosts;
        return '<div class="system-row"><span class="system-icon">' + icon('cluster') + '</span><div><h3>' + esc(c.name) + '</h3><p>' + c.hosts + ' host' + (c.hosts !== 1 ? 's' : '') + ' · ' + c.vms_on + ' of ' + c.vms + ' VMs running · HA ' + (c.ha ? 'on' : 'off') + ' · DRS ' + (c.drs ? 'on' : 'off') + '</p></div>' +
          '<span class="state ' + (ok ? '' : 'warn') + '"><span class="dot"></span> ' + (ok ? 'Online' : (c.hosts - c.hosts_available) + ' down') + '</span></div>';
      }).join('') + '</article></div></section>';
  }

  function clusterPages(d) {
    var db = d.dbh;
    return chunk(db.clusters, per('clusters')).map(function (list) {
      return '<section class="page" data-section="clusters">' + heading('CLUSTER CAPACITY', 'Clusters.') +
        '<article class="card hv-list hv-clusters' + scrolls('clusters') + '"><div class="card-top"><h2>All clusters</h2><span class="hv-tools">' + showPicker('clusters', d) + '</span></div><div class="hv-list-head"><span>Cluster</span><span>Hosts</span><span>VMs on</span><span>vCPU : core</span><span>Running vRAM % of DRAM</span><span>N+1</span><span>Datastore free</span></div>' +
        list.map(function (c) {
          return '<div class="hv-row"><div class="hv-name"><span class="system-icon">' + icon('cluster') + '</span><div><h3>' + esc(c.name) + '</h3><p>HA ' + (c.ha ? 'on' : 'off') + ' · DRS ' + (c.drs ? 'on' : 'off') + ' · ' + c.cores + ' cores · ' + gib(c.dram_gib) + '</p></div></div>' +
            '<span class="hv-big">' + c.hosts_available + '<small>/' + c.hosts + '</small></span><span class="hv-big">' + c.vms_on + '<small>/' + c.vms + '</small></span>' +
            '<span class="hv-big">' + num(c.vcpu_core, 2) + '</span>' +
            '<div class="hv-bar"><div class="metric-line"><strong>' + pct(c.vram_pct) + '<small>%</small></strong></div>' + meter(c.vram_pct, 80, 100) + '</div>' +
            '<span class="pill ' + (c.n1 ? 'ok' : 'crit') + '">' + (c.n1 ? 'Yes' : 'No') + '</span>' +
            '<span class="hv-big ' + level(100 - (c.ds_free_pct || 0)) + '-text">' + pct(c.ds_free_pct) + '<small>%</small></span></div>';
        }).join('') + '</article></section>';
    });
  }

  function hostPages(d) {
    var db = d.dbh;
    return chunk(sortedList('hosts', d), per('hosts')).map(function (list) {
      return '<section class="page" data-section="hosts">' + heading('ESX HOSTS', 'Hosts.') +
        '<article class="card hv-list hv-hosts' + scrolls('hosts') + '"><div class="card-top"><h2>All hosts</h2><span class="hv-tools">' + showPicker('hosts', d) + sortMenu('hosts', 'hosts') + '</span></div><div class="hv-list-head"><span>Host</span><span>CPU</span><span>Memory</span><span>VMs</span><span>State</span></div>' +
        list.map(function (h) {
          var st = h.state !== 'connected' ? ['crit', h.state] : h.maintenance ? ['warn', 'Maintenance'] : ['ok', 'Online'];
          return '<div class="hv-row"><div class="hv-name"><span class="system-icon">' + icon('server') + '</span><div><h3>' + esc(h.name) + '</h3><p>' + esc(h.cluster || '') + ' · ' + esc(h.version) + '</p></div></div>' +
            '<div class="hv-bar"><div class="metric-line"><strong>' + pct(h.cpu_pct) + '<small>%</small></strong><span>' + h.cores + ' cores</span></div>' + meter(h.cpu_pct) + '</div>' +
            '<div class="hv-bar"><div class="metric-line"><strong>' + pct(h.mem_pct) + '<small>%</small></strong><span>' + gib(h.dram_gib) + '</span></div>' + meter(h.mem_pct) + '</div>' +
            '<span class="hv-big">' + h.vms + '<small>/' + h.vms_total + '</small></span>' +
            '<span class="state ' + st[0] + '"><span class="dot"></span> ' + esc(st[1]) + '</span></div>';
        }).join('') + '</article></section>';
    });
  }

  function vmPage(d) {
    var db = d.dbh, t = db.totals;
    return '<section class="page" data-section="vms">' + heading('VIRTUAL MACHINES', 'Workloads.') +
      '<div class="hv-stack">' +
      '<article class="card hv-stats"><div><strong>' + t.vms_on + '</strong><span>Running</span></div><div><strong>' + t.vms_off + '</strong><span>Powered off</span></div>' +
      '<div><strong>' + t.templates + '</strong><span>Templates</span></div><div><strong class="' + (t.snapshots ? 'warn-text' : '') + '">' + t.snapshots + '</strong><span>Snapshots</span></div>' +
      '<div><strong class="' + (t.tools_upgrade ? 'warn-text' : '') + '">' + t.tools_upgrade + '</strong><span>Tools to upgrade</span></div></article>' +
      '<article class="card hv-list hv-vms' + scrolls('vms') + '"><div class="card-top"><h2>Busiest right now</h2><span class="hv-tools">' + showPicker('vms', d) + sortMenu('vms', 'VMs') + '</span></div>' +
      '<div class="hv-list-head"><span>Virtual machine</span><span>CPU</span><span>Memory</span><span>Size</span></div>' +
      sortedList('vms', d).slice(0, per('vms')).map(function (v) {
        return '<div class="hv-row"><div class="hv-name"><span class="system-icon">' + icon('vm') + '</span><div><h3>' + esc(v.name) + '</h3><p>' + esc(v.os || '') + '</p></div></div>' +
          '<div class="hv-bar"><div class="metric-line"><strong>' + pct(v.cpu_pct) + '<small>%</small></strong></div>' + meter(v.cpu_pct) + '</div>' +
          '<div class="hv-bar"><div class="metric-line"><strong>' + pct(v.mem_pct) + '<small>%</small></strong></div>' + meter(v.mem_pct) + '</div>' +
          '<span class="hv-size">' + v.cpus + ' vCPU · ' + num(v.mem_gib, 1) + ' GiB</span></div>';
      }).join('') + '</article></div></section>';
  }

  function healthPages(d) {
    var db = d.dbh;
    var types = db.findings_by_type.slice(0, 7), max = Math.max.apply(null, types.map(function (x) { return x.count; }).concat([1]));
    var pages = chunk(db.findings, per('health'));
    return pages.map(function (list, i) {
      return '<section class="page" data-section="health">' + heading('DBH INSIGHTS HEALTH CHECK', db.findings.length ? 'Health findings.' : 'All clear.') +
        '<div class="hv-two">' +
        '<article class="card hv-types"><div class="card-top"><h2>By type</h2><span class="tag">' + db.totals.findings + ' TOTAL</span></div>' +
        (types.length ? types.map(function (f) {
          return '<div class="hv-type"><div class="metric-line"><span>' + esc(f.type) + '</span><strong>' + f.count + '</strong></div><div class="meter hv-meter warn"><i style="width:' + (100 * f.count / max) + '%"></i></div></div>';
        }).join('') : '<p class="hv-empty">No findings.</p>') + '</article>' +
        '<article class="card hv-list hv-findings' + scrolls('health') + '"><div class="card-top"><h2>Findings</h2><span class="hv-tools">' + showPicker('health', d) + (pages.length > 1 ? '<span class="tag">' + (i + 1) + ' OF ' + pages.length + '</span>' : '') + '</span></div>' +
        (list.length ? list.map(function (f) {
          return '<div class="hv-row"><span class="event-dot-lg gold"></span><div><h3>' + esc(f.object) + '</h3><p>' + esc(f.message) + '</p></div><span class="tag">' + esc(f.type) + '</span></div>';
        }).join('') : '<p class="hv-empty">Every check passed.</p>') + '</article></div></section>';
    });
  }

  function storagePages(d) {
    var st = d.storage, t = st.totals;
    return chunk(sortedList('storage', d), per('storage')).map(function (list) {
      return '<section class="page" data-section="storage">' + heading('DATASTORE CAPACITY', 'Storage.') +
        '<div class="hv-stack">' +
        '<article class="card hv-stats"><div><strong>' + num(t.capacity_tb, 1) + '<small> TB</small></strong><span>Total capacity</span></div>' +
        '<div><strong>' + num(t.free_tb, 1) + '<small> TB</small></strong><span>Free</span></div>' +
        '<div><strong class="' + (t.lowest_free_pct < 10 ? 'crit-text' : t.lowest_free_pct < 25 ? 'warn-text' : '') + '">' + pct(t.lowest_free_pct) + '<small>%</small></strong><span>Lowest free</span></div>' +
        '<div><strong class="' + (t.over_80 ? 'crit-text' : '') + '">' + t.over_80 + '</strong><span>Over 80 % used</span></div></article>' +
        '<article class="card hv-list hv-ds' + scrolls('storage') + '"><div class="card-top"><h2>Datastores</h2><span class="hv-tools">' + showPicker('storage', d) + sortMenu('storage', 'datastores') + '</span></div><div class="hv-list-head"><span>Datastore</span><span>Used</span><span>Free</span><span>Runway</span></div>' +
        list.map(function (x) {
          var rw = x.runway_days == null ? '<span class="hv-muted">No growth</span>' : '<span class="hv-big ' + (x.runway_days < 30 ? 'crit-text' : x.runway_days < 60 ? 'warn-text' : '') + '">' + num(x.runway_days) + '<small> days</small></span>';
          return '<div class="hv-row"><div class="hv-name"><span class="system-icon">' + icon('disk') + '</span><div><h3>' + esc(x.name) + '</h3><p>' + esc((x.type || '').toUpperCase()) + ' · ' + esc(x.datacenter || '') + ' · ' + (x.vms || 0) + ' VMs</p></div></div>' +
            '<div class="hv-bar"><div class="metric-line"><strong>' + pct(x.used_pct) + '<small>%</small></strong><span>of ' + num(x.capacity_tb, 2) + ' TB</span></div>' + meter(x.used_pct, 80, 90) + '</div>' +
            '<span class="hv-size">' + num(x.free_tb, 2) + ' TB</span>' + rw + '</div>';
        }).join('') + '</article></div></section>';
    });
  }

  function githubPage(d) {
    var gh = d.github, s = gh.status;
    var ind = { none: ['ok', 'All systems operational'], minor: ['warn', 'Minor issues'], major: ['crit', 'Major outage'], critical: ['crit', 'Critical outage'], maintenance: ['info', 'Maintenance'] }[s.indicator] || ['info', s.indicator];
    var compState = function (c) { return c === 'operational' ? 'ok' : c === 'degraded_performance' ? 'warn' : c === 'under_maintenance' ? 'info' : 'crit'; };
    var inc = gh.incidents.length ? gh.incidents.slice(0, 2).map(function (i) {
      return '<div class="hv-row hv-incident"><span class="event-dot-lg ' + (i.impact === 'major' || i.impact === 'critical' ? 'red' : 'gold') + '"></span><div><h3>' + esc(i.name) + '</h3><p>' + esc(i.latest_update || '') + '</p></div><span class="tag">' + esc((i.status || '').toUpperCase()) + '</span></div>';
    }).join('') : (gh.maintenances.length ? gh.maintenances.slice(0, 2).map(function (m) {
      return '<div class="hv-row hv-incident"><span class="event-dot-lg blue"></span><div><h3>' + esc(m.name) + '</h3><p>Scheduled ' + esc(shortDate(m.scheduled_for)) + ' · ' + esc(m.components || '') + '</p></div><span class="tag">MAINTENANCE</span></div>';
    }).join('') : '<p class="hv-empty">No incidents or maintenance.</p>');
    return '<section class="page" data-section="github">' + heading('AN EXAMPLE OUTSIDE THE LAB', 'GitHub status.') +
      '<div class="hv-stack">' +
      '<article class="card lab-banner hv-gh-banner"><div><span class="eyebrow">GITHUBSTATUS.COM</span><h2>' + esc(s.description || ind[1]) + '</h2><p>Updated ' + esc(ago(s.updated_at)) + ' · collected ' + esc(ago(gh.collected_at)) + '</p></div>' +
      '<div class="health-badge ' + ind[0] + '"><span class="dot"></span> ' + esc(ind[1]) + '</div></article>' +
      '<article class="card hv-components"><div class="card-top"><h2>Components</h2><span class="tag live-tag">LIVE</span></div><div class="hv-comp-grid">' +
      gh.components.slice(0, 10).map(function (c) {
        var k = compState(c.status);
        return '<div class="hv-comp"><span>' + esc(c.name) + '</span><span class="state ' + k + '"><span class="dot"></span> ' + esc((c.status || '').replace(/_/g, ' ')) + '</span></div>';
      }).join('') + '</div></article>' +
      '<article class="card hv-list hv-incidents"><div class="card-top"><h2>' + (gh.incidents.length ? 'Active incidents' : 'Coming up') + '</h2>' + more(gh.incidents.length - 2) + '</div>' + inc + '</article>' +
      '</div></section>';
  }

  function unavailablePage(section, title, why) {
    return '<section class="page" data-section="' + section + '">' + heading('NOT CONNECTED', title) +
      '<article class="card hv-unavailable"><h2>' + esc(why) + '</h2><p>Turn the module on, or wait for its first collection, on the Modules page.</p></article></section>';
  }

  // -- sections + navigation ---------------------------------------------------------
  // Default order; the Settings page can reorder and hide pages (data.layout = {order, hidden}).
  var ALL_SECTIONS = [
    { id: 'home', label: 'Home', icon: 'home' },
    { id: 'lab', label: 'Lab', icon: 'lab' },
    { id: 'hosts', label: 'Hosts', icon: 'server' },
    { id: 'vms', label: 'VMs', icon: 'vm' },
    { id: 'storage', label: 'Storage', icon: 'disk' },
    { id: 'clusters', label: 'Clusters', icon: 'cluster' },
    { id: 'health', label: 'Health', icon: 'heart' },
    { id: 'github', label: 'GitHub', icon: 'branch' }
  ];
  var SECTIONS = ALL_SECTIONS;
  function applyLayout(lay) {
    if (!lay || !lay.order) { SECTIONS = ALL_SECTIONS; return; }
    var byId = {}; ALL_SECTIONS.forEach(function (s) { byId[s.id] = s; });
    var order = lay.order.filter(function (id) { return byId[id]; });
    ALL_SECTIONS.forEach(function (s) { if (order.indexOf(s.id) < 0) order.push(s.id); });
    var shown = order.filter(function (id) { return (lay.hidden || []).indexOf(id) < 0; });
    SECTIONS = (shown.length ? shown : order).map(function (id) { return byId[id]; });
  }
  var pages = [];      // [{section, sub, html}]
  var current = 0, data = null;

  function build(d) {
    applyLayout(d.layout);
    var by = { home: [homePage(d)] };
    if (d.dbh.available) {
      by.lab = [labPage(d)];
      if (d.dbh.clusters.length) by.clusters = clusterPages(d);
      by.hosts = hostPages(d);
      by.vms = [vmPage(d)];
      by.health = healthPages(d);
    } else by.lab = [unavailablePage('lab', 'Home lab.', d.dbh.why)];
    if (d.storage.available) by.storage = storagePages(d);
    if (d.github.available) by.github = [githubPage(d)];
    var out = [];
    SECTIONS.forEach(function (s) { (by[s.id] || []).forEach(function (html, i) { out.push({ section: s.id, sub: i + 1, html: html }); }); });
    return out;
  }

  function hashFor(p) { return '#' + p.section + (p.sub > 1 ? '-' + p.sub : ''); }
  function indexFromHash() {
    var m = /^#([a-z]+)(?:-(\d+))?$/.exec(location.hash || '');
    if (!m) return 0;
    for (var i = 0; i < pages.length; i++) if (pages[i].section === m[1] && pages[i].sub === Number(m[2] || 1)) return i;
    for (i = 0; i < pages.length; i++) if (pages[i].section === m[1]) return i;
    return 0;
  }

  // Rebuild the pages when the screen switches between the two layouts (rotate, resize).
  function onLayout() {
    var next = shortQuery.matches ? SHORT : FULL;
    if (next !== PER) { PER = next; if (data) render(true); }
  }
  if (shortQuery.addEventListener) shortQuery.addEventListener('change', onLayout); else if (shortQuery.addListener) shortQuery.addListener(onLayout);

  function render(keepCurrent) {
    var keep = keepCurrent && pages[current] ? { section: pages[current].section, sub: pages[current].sub } : null;
    pages = build(data);
    main.innerHTML = pages.map(function (p) { return p.html; }).join('');
    var present = SECTIONS.filter(function (s) { return pages.some(function (p) { return p.section === s.id; }); });
    navBox.innerHTML = present.map(function (s) {
      var n = pages.filter(function (p) { return p.section === s.id; }).length;
      return '<a href="#' + s.id + '" class="nav-item" data-nav="' + s.id + '">' + icon(s.icon) + '<span>' + s.label + '</span>' + (n > 1 ? '<small>' + n + ' pages</small>' : '') + '</a>';
    }).join('');
    navBox.style.setProperty('--n', present.length);
    if (keep) {
      current = 0;
      for (var i = 0; i < pages.length; i++) if (pages[i].section === keep.section && pages[i].sub <= keep.sub) current = i;
    } else current = indexFromHash();
    show(current, true);
    updateClock();
  }

  function show(i, quiet) {
    current = (i + pages.length) % pages.length;
    var nodes = main.children, p = pages[current];
    for (var k = 0; k < nodes.length; k++) {
      nodes[k].hidden = k !== current;
      var pc = nodes[k].querySelector('.page-count');
      if (pc) { pc.querySelector('.pc-now').textContent = pad2(k + 1); pc.querySelector('.pc-all').textContent = '/ ' + pad2(pages.length); }
    }
    navBox.querySelectorAll('.nav-item').forEach(function (a) {
      var on = a.dataset.nav === p.section;
      a.classList.toggle('active', on);
      if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    syncHeader();
    if (location.hash !== hashFor(p)) history.replaceState(history.state, '', hashFor(p));
    var label = (ALL_SECTIONS.find(function (s) { return s.id === p.section; }) || {}).label;
    document.title = label + ' · ' + OWNER + '’s HomeView';
    note();
    if (!quiet) resetCycle();
  }

  // The page title and page count sit in the top row, beside "HomeView" (the in-page heading is hidden).
  var headTitle = document.getElementById('hv-page-title');
  function syncHeader() {
    var node = main.children[current]; if (!node) return;
    var h = node.querySelector('.page-heading h1');
    var text = h ? h.textContent : '';
    if (headTitle.textContent !== text) headTitle.textContent = text;
  }

  // Show menu (in each long list's header row): rows per page for that section.
  main.addEventListener('change', function (e) {
    var sorter = e.target.closest && e.target.closest('select[data-sort]');
    if (sorter) {
      sortBy[sorter.dataset.sort] = sorter.value;
      try { localStorage.setItem('homeview.sort', JSON.stringify(sortBy)); } catch (e2) {}
      if (pages[current]) pages[current].sub = 1;
      render(true);
      return;
    }
    var sel = e.target.closest && e.target.closest('select[data-show]'); if (!sel) return;
    var sec = sel.dataset.show;
    if (sel.value === 'fit') delete showRows[sec]; else showRows[sec] = Number(sel.value);
    try { localStorage.setItem('homeview.show', JSON.stringify(showRows)); } catch (e2) {}
    if (pages[current]) pages[current].sub = 1;   // start the section again at its first page
    render(true);
  });
  // Don't let a tap on the menu count as a swipe or start the page switching.
  main.addEventListener('focusin', function (e) { if (e.target.matches && e.target.matches('select[data-show], select[data-sort]') && cycling) resetCycle(); });
  // Scrolling a long list restarts the Auto-cycle timer, so the page doesn't change under you.
  main.addEventListener('scroll', function () { if (cycling) resetCycle(); }, true);

  function note() {
    if (!data) return;
    var p = pages[current], src = p.section === 'storage' ? data.storage : p.section === 'github' ? data.github : p.section === 'home' ? null : data.dbh;
    var text = p.section === 'home' ? 'Clock is live. Lab data refreshes every minute.' :
      src && src.available ? 'Live data · collected ' + ago(src.collected_at) + ' · refreshes every minute' : (src && src.why) || '';
    if (data.mock) text += ' · SAMPLE DATA (VCO_MOCK)';
    document.getElementById('data-note').textContent = text;
  }

  // -- clock (unchanged logic from HomeView) ---------------------------------------
  function updateClock() {
    var now = new Date();
    var parts = new Intl.DateTimeFormat('en-US', { timeZone: TIMEZONE, hour: 'numeric', minute: '2-digit', hour12: true }).formatToParts(now);
    var part = function (t) { var x = parts.find(function (p) { return p.type === t; }); return x ? x.value : ''; };
    var hour = Number(new Intl.DateTimeFormat('en-US', { hour: 'numeric', hourCycle: 'h23', timeZone: TIMEZONE }).format(now));
    document.querySelectorAll('.hv-clock').forEach(function (c) { c.textContent = part('hour') + ':' + part('minute'); c.dateTime = now.toISOString(); });
    document.querySelectorAll('.hv-period').forEach(function (c) { c.textContent = part('dayPeriod'); });
    document.querySelectorAll('.hv-weekday').forEach(function (c) { c.textContent = new Intl.DateTimeFormat('en-US', { weekday: 'long', timeZone: TIMEZONE }).format(now); });
    var eb = document.getElementById('home-eyebrow'), ti = document.getElementById('home-title');
    if (eb) eb.textContent = new Intl.DateTimeFormat('en-US', { weekday: 'long', month: 'long', day: 'numeric', timeZone: TIMEZONE }).format(now).toUpperCase();
    if (ti) ti.textContent = 'Good ' + (hour < 12 ? 'morning' : hour < 17 ? 'afternoon' : 'evening') + ', ' + OWNER + '.';
    if (pages.length) syncHeader();
  }
  setInterval(updateClock, 1000);

  // -- auto-cycle (seconds per page from the Settings page; on/off remembered on this screen)
  var cycleTimer = null, cycling = true;
  try { cycling = localStorage.getItem('homeview.cycle') !== 'off'; } catch (e) {}
  var rotate = document.getElementById('rotate');
  function resetCycle() {
    clearInterval(cycleTimer);
    rotate.setAttribute('aria-pressed', String(cycling));
    if (!cycling || pages.length < 2) return;
    cycleTimer = setInterval(function () { show(current + 1, true); }, CYCLE_MS);
  }
  var toastTimer;
  function toast(message) {
    var node = document.getElementById('toast');
    node.textContent = message; node.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { node.hidden = true; }, 5000);
  }
  rotate.addEventListener('click', function () {
    cycling = !cycling;
    try { localStorage.setItem('homeview.cycle', cycling ? 'on' : 'off'); } catch (e) {}
    resetCycle();
    toast(cycling ? 'Auto-cycle on · pages switch every ' + CYCLE_MS / 1000 + ' seconds' : 'Auto-cycle paused');
  });
  navBox.addEventListener('click', function (e) {
    var a = e.target.closest('.nav-item'); if (!a) return;
    e.preventDefault();
    var i = pages.findIndex(function (p) { return p.section === a.dataset.nav; });
    // Tapping the section you're on goes to its next page (Hosts 1 -> 2 -> 1).
    if (pages[current].section === a.dataset.nav && pages[current + 1] && pages[current + 1].section === a.dataset.nav) i = current + 1;
    show(i);
    closeMenu();
  });

  // -- dashboards menu: the home icon (top left) opens it; a pick, a tap outside or Esc closes it.
  var menu = document.getElementById('hv-menu'), menuBtn = document.getElementById('menu-btn');
  function openMenu() {
    menu.hidden = false; menuBtn.setAttribute('aria-expanded', 'true');
    var a = navBox.querySelector('.nav-item.active') || navBox.querySelector('.nav-item');
    if (a) a.focus({ preventScroll: true });
  }
  function closeMenu(refocus) {
    if (menu.hidden) return;
    menu.hidden = true; menuBtn.setAttribute('aria-expanded', 'false');
    if (refocus) menuBtn.focus();
  }
  menuBtn.addEventListener('click', function (e) { e.stopPropagation(); if (menu.hidden) openMenu(); else closeMenu(); });
  document.addEventListener('click', function (e) { if (!menu.hidden && !menu.contains(e.target)) closeMenu(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeMenu(true); });
  window.addEventListener('hashchange', function () { var i = indexFromHash(); if (i !== current) show(i); });
  document.addEventListener('keydown', function (e) {
    if (!menu.hidden) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        var items = [].slice.call(navBox.querySelectorAll('.nav-item')), at = items.indexOf(document.activeElement);
        var next = items[(at + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length];
        if (next) { e.preventDefault(); next.focus(); }
      }
      return;
    }
    if (e.key === 'ArrowRight') show(current + 1);
    else if (e.key === 'ArrowLeft') show(current - 1);
  });
  // Swipe left / right on a touch screen.
  var x0 = null;
  display.addEventListener('touchstart', function (e) { x0 = menu.hidden ? e.touches[0].clientX : null; }, { passive: true });
  display.addEventListener('touchend', function (e) {
    if (x0 == null) return;
    var dx = e.changedTouches[0].clientX - x0; x0 = null;
    if (Math.abs(dx) > 80) show(current + (dx < 0 ? 1 : -1));
  }, { passive: true });
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) clearInterval(cycleTimer); else { updateClock(); fitDisplay(); load(); resetCycle(); }
  });

  // -- data ---------------------------------------------------------------------------
  var edition = document.getElementById('edition');
  function load() {
    fetch('/ui/homeview/data', { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (d) {
      var first = !data;
      data = d;
      var ms = Math.max(5, Math.min(3600, Number(d.cycle_seconds) || 20)) * 1000;
      rotate.title = 'Show every page in turn, ' + ms / 1000 + ' seconds each';
      if (ms !== CYCLE_MS) { CYCLE_MS = ms; if (!first) resetCycle(); }
      render(!first);
      edition.hidden = true; // the status shows only when the data can't load
      if (first) resetCycle();
    }).catch(function (e) {
      edition.hidden = false;
      if (!data) document.getElementById('data-note').textContent = 'Could not load the lab data (' + e.message + '). Retrying every minute.';
    });
  }
  load();
  setInterval(load, REFRESH_MS);
})();
