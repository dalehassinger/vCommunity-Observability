/* About page, shared by the dashboards (/static/about.js) and Ask (/shared/about.js).
   Renders README.md (passed as JSON in #about-md) with the vendored marked + DOMPurify,
   gives headings GitHub-style ids so README links work, turns GitHub callouts
   ("> [!WARNING]") into alert boxes, points screenshots at the app and repository
   files at GitHub, and builds the "On this page" list. Works offline. */
(function () {
  var cfg = window.ABOUT || {};
  var holder = document.getElementById('about-md');
  var body = document.getElementById('about-body');
  if (!holder || !body) return;
  var md = JSON.parse(holder.textContent || '""');
  if (!md) { body.innerHTML = '<p class="subtle">README.md was not found in this image.</p>'; return; }
  body.innerHTML = DOMPurify.sanitize(marked.parse(md));

  // The page header already shows the project name.
  var h1 = body.querySelector('h1');
  if (h1 && h1 === body.firstElementChild) h1.remove();

  // GitHub-style heading ids (same rules as the README's own links).
  var used = {};
  function slug(text) {
    var s = text.trim().toLowerCase().replace(/[^\w\- ]+/g, '').replace(/ /g, '-');
    var base = s, n = 1;
    while (used[s]) s = base + '-' + n++;
    used[s] = true;
    return s;
  }
  var heads = body.querySelectorAll('h2, h3, h4');
  heads.forEach(function (h) { h.id = slug(h.textContent); });

  // GitHub callouts.
  body.querySelectorAll('blockquote').forEach(function (q) {
    var p = q.querySelector('p');
    var m = p && p.innerHTML.match(/^\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*(<br>)?\s*/i);
    if (!m) return;
    var kind = m[1].toLowerCase();
    p.innerHTML = p.innerHTML.slice(m[0].length);
    q.className = 'about-callout about-callout-' + kind;
    var label = document.createElement('div');
    label.className = 'about-callout-label';
    label.textContent = kind.charAt(0).toUpperCase() + kind.slice(1);
    q.insertBefore(label, q.firstChild);
  });

  // Links: anchors stay, web links open in a new tab, repository files go to GitHub.
  body.querySelectorAll('a[href]').forEach(function (a) {
    var href = a.getAttribute('href');
    if (href.charAt(0) === '#') return;
    if (/^(https?:|mailto:)/i.test(href)) { a.target = '_blank'; a.rel = 'noopener'; return; }
    var path = href.replace(/^\.?\//, '');
    if (cfg.repo) { a.href = cfg.repo + '/blob/main/' + path; a.target = '_blank'; a.rel = 'noopener'; }
    else { a.removeAttribute('href'); }
  });

  // Screenshots are served by the app; other relative images are dropped.
  body.querySelectorAll('img[src]').forEach(function (img) {
    var src = img.getAttribute('src');
    if (/^(https?:|data:)/i.test(src)) return;
    var m = src.match(/^\.?\/?docs\/screenshots\/([\w.-]+\.(png|jpe?g|gif|svg|webp))$/i);
    if (m && cfg.screenshots) { img.src = cfg.screenshots + m[1]; img.loading = 'lazy'; }
    else { img.remove(); }
  });
  body.querySelectorAll('table').forEach(function (t) {
    var w = document.createElement('div'); w.className = 'about-table';
    t.parentNode.insertBefore(w, t); w.appendChild(t);
  });

  // "On this page".
  var toc = document.getElementById('about-toc');
  if (toc) {
    var links = {};
    body.querySelectorAll('h2, h3').forEach(function (h) {
      var a = document.createElement('a');
      a.href = '#' + h.id; a.textContent = h.textContent;
      a.className = h.tagName === 'H3' ? 'sub' : '';
      toc.appendChild(a); links[h.id] = a;
    });
    if ('IntersectionObserver' in window) {
      var current = null;
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (!e.isIntersecting) return;
          if (current) current.classList.remove('active');
          current = links[e.target.id]; if (current) current.classList.add('active');
        });
      }, { rootMargin: '0px 0px -75% 0px' });
      body.querySelectorAll('h2, h3').forEach(function (h) { io.observe(h); });
    }
  }

  // Filter: hide sections that don't mention the text.
  var filter = document.getElementById('about-filter');
  if (filter) {
    filter.addEventListener('input', function () {
      var q = filter.value.trim().toLowerCase();
      var show = !q, hidden = {}, section = null;
      Array.prototype.forEach.call(body.children, function (el) {
        if (el.tagName === 'H2') {
          var text = el.textContent, n = el.nextElementSibling;
          while (n && n.tagName !== 'H2') { text += ' ' + n.textContent; n = n.nextElementSibling; }
          show = !q || text.toLowerCase().indexOf(q) >= 0;
          section = el.id;
        }
        if (el.id) hidden[el.id] = !show;
        el.style.display = show ? '' : 'none';
      });
      if (toc) Array.prototype.forEach.call(toc.children, function (a) {
        a.style.display = hidden[a.getAttribute('href').slice(1)] ? 'none' : '';
      });
    });
  }
  if (location.hash) {
    var t = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (t) t.scrollIntoView();
  }
})();
