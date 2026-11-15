/* Small screens: the side menu is a slide-out drawer (body.nav-open). This adds a dimmed
   backdrop that closes it on a click, closes it with Escape, and keeps aria-expanded on the
   menu button in step. Shared by both containers (Ask serves it at /shared/drawer.js). */
(function () {
  var body = document.body;
  var toggle = document.querySelector('.nav-toggle');
  var backdrop = document.createElement('div');
  backdrop.className = 'nav-backdrop';
  backdrop.setAttribute('aria-hidden', 'true');
  body.appendChild(backdrop);
  function close() { body.classList.remove('nav-open'); }
  backdrop.addEventListener('click', close);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && body.classList.contains('nav-open')) {
      close();
      if (toggle) toggle.focus();
    }
  });
  var small = window.matchMedia('(max-width: 860px)');
  // Only on small screens: on wide screens Ask's menu button hides the sidebar instead.
  function sync() { if (toggle && small.matches) toggle.setAttribute('aria-expanded', body.classList.contains('nav-open') ? 'true' : 'false'); }
  sync();
  new MutationObserver(sync).observe(body, { attributes: true, attributeFilter: ['class'] });
})();
