/* The Sources picker (chat, Saved prompts form, Scheduled prompts form): one copy of its rules.
   Each source is a check box styled as a button (templates/_scope_rows.html).
   - Nothing ticked = All sources (every source except spreadsheets), sent as "all".
   - "No sources (just the model)" and knowledge sources exclude each other; web search, web
     fetch and MCP tools may go with either (the same rule as main.split_scope on the server).
   - "All sources" clears the ticks; "only these" ticks one group and nothing else.
   AskScope.bind(panel, { label, onChange(byUser) }) returns { boxes, picked, value, set, refresh }. */
(function () {
  'use strict';
  var ALL_TEXT = 'All sources (not spreadsheets)';

  function isTool(b) { var p = b.getAttribute('data-plugin'); return p === 'web' || p === 'mcp'; }
  function nameOf(b) { return b.parentNode.textContent.trim(); }

  function bind(panel, opts) {
    opts = opts || {};
    var label = opts.label || null;
    function boxes() { return Array.prototype.slice.call(panel.querySelectorAll('input[type=checkbox]')); }
    function ticked() { return boxes().filter(function (b) { return b.checked; }); }
    function picked() { return ticked().map(function (b) { return b.value; }); }
    function value() { var p = picked(); return p.length ? p : 'all'; }
    function refresh(byUser) {
      var on = ticked(), names = on.map(nameOf);
      if (label) {
        label.textContent = !on.length ? ALL_TEXT : on.length <= 3 ? names.join(', ')
          : on.length + ' picked: ' + names.slice(0, 2).join(', ') + '…';
        label.title = on.length ? names.join(', ') : 'Every source except spreadsheets';
      }
      var allBtn = panel.querySelector('[data-scope-all]');
      if (allBtn) allBtn.setAttribute('aria-pressed', on.length ? 'false' : 'true');
      if (opts.onChange) opts.onChange(!!byUser);
    }
    function set(values) {
      var want = Array.isArray(values) ? values : String(values || '').split(',');
      boxes().forEach(function (b) { b.checked = want.indexOf(b.value) >= 0; });
      refresh(false);
    }
    panel.addEventListener('change', function (e) {
      var t = e.target;
      if (!t || t.type !== 'checkbox') return;
      if (t.checked && t.value === 'none') {
        boxes().forEach(function (b) { if (b !== t && !isTool(b)) b.checked = false; });
      } else if (t.checked && !isTool(t)) {
        boxes().forEach(function (b) { if (b.value === 'none') b.checked = false; });
      }
      refresh(true);
    });
    panel.addEventListener('click', function (e) {
      var t = e.target.closest('[data-scope-all],[data-scope-only]');
      if (!t) return;
      var only = t.getAttribute('data-scope-only');
      boxes().forEach(function (b) { b.checked = !!only && b.getAttribute('data-plugin') === only; });
      refresh(true);
    });
    return { boxes: boxes, picked: picked, value: value, set: set, refresh: refresh };
  }

  window.AskScope = { bind: bind, ALL_TEXT: ALL_TEXT };
})();
