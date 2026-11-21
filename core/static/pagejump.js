/* Page jump buttons (both apps): two small arrow buttons at the bottom right that jump to the
   top or the bottom of a long page. Each one shows only when there is somewhere to go
   (more than 300 px above or below). Not on the Ask chat page: it has its own Jump to latest
   button on the composer. Smooth scrolling unless the reader prefers reduced motion. */
(function () {
  'use strict';
  if (document.body && document.body.classList.contains('chat-page')) return;
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
  }
  function later() { if (!queued) { queued = true; requestAnimationFrame(update); } }

  function start() {
    document.body.appendChild(box);
    window.addEventListener('scroll', later, { passive: true });
    window.addEventListener('resize', later);
    // Pages that grow after load (a table filled by script): watch the content area only, not
    // the whole body, so menus, toasts and the sidebar don't wake it.
    var main = document.querySelector('main') || document.body;
    if (window.ResizeObserver) new ResizeObserver(later).observe(main);
    update();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
