/* Page jump buttons (both apps): two small arrow buttons at the bottom right that jump to the
   top or the bottom of a long page. Each one shows only when there is somewhere to go
   (more than 300 px above or below). On a page with a sticky composer (the Ask chat), the
   buttons sit just above it. Smooth scrolling unless the reader prefers reduced motion. */
(function () {
  'use strict';
  var GAP = 300;
  var box = document.createElement('div');
  box.className = 'page-jump';
  box.innerHTML =
    '<button type="button" class="page-jump-btn" data-to="top" aria-label="Jump to the top of the page" title="Top of the page" hidden>' +
    '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>' +
    '<button type="button" class="page-jump-btn" data-to="bottom" aria-label="Jump to the bottom of the page" title="Bottom of the page" hidden>' +
    '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M12 5v14M5 12l7 7 7-7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>';
  var up = box.children[0], down = box.children[1];
  var smooth = !(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  function root() { return document.scrollingElement || document.documentElement; }
  function go(top) { window.scrollTo({ top: top, behavior: smooth ? 'smooth' : 'auto' }); }
  up.addEventListener('click', function () { go(0); });
  down.addEventListener('click', function () { go(root().scrollHeight); });

  var queued = false;
  function update() {
    queued = false;
    var r = root(), y = window.scrollY || r.scrollTop;
    up.hidden = y < GAP;
    down.hidden = r.scrollHeight - (y + window.innerHeight) < GAP;
    var composer = document.querySelector('.composer');
    box.style.bottom = composer ? (composer.offsetHeight + 8) + 'px' : '';
  }
  function later() { if (!queued) { queued = true; requestAnimationFrame(update); } }

  function start() {
    document.body.appendChild(box);
    window.addEventListener('scroll', later, { passive: true });
    window.addEventListener('resize', later);
    // Pages that grow after load (a streaming answer, a table filled by script).
    if (window.ResizeObserver) new ResizeObserver(later).observe(document.body);
    update();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
