/* vCommunity Ask sidebar: select several recent chats and delete them together.
   Select shows a check box on each chat; clicking a chat then ticks it instead of opening it
   (Shift+click ticks a range). Delete asks once, then removes the ticked chats. */
(function () {
  'use strict';
  var list = document.getElementById('chat-list');
  var selectBtn = document.getElementById('chat-select');
  var bar = document.getElementById('chat-bar');
  if (!list || !bar) return;
  var all = document.getElementById('chat-all');
  var del = document.getElementById('chat-del');
  var cancel = document.getElementById('chat-cancel');
  var boxes = [];
  var lastIndex = -1;
  // Read the boxes each time: the chat page adds a row when a new chat starts.
  function scan() { boxes = Array.prototype.slice.call(list.querySelectorAll('.chat-check')); }

  function ticked() { return boxes.filter(function (b) { return b.checked; }); }
  function refresh() {
    var n = ticked().length;
    del.disabled = n === 0;
    del.textContent = n ? 'Delete (' + n + ')' : 'Delete';
    all.checked = n > 0 && n === boxes.length;
    all.indeterminate = n > 0 && n < boxes.length;
  }
  function setMode(on) {
    scan();
    list.classList.toggle('selecting', on);
    bar.hidden = !on;
    if (selectBtn) selectBtn.hidden = on;
    boxes.forEach(function (b) { b.checked = false; b.tabIndex = on ? 0 : -1; });
    lastIndex = -1;
    refresh();
  }

  if (selectBtn) selectBtn.addEventListener('click', function () { setMode(true); });
  cancel.addEventListener('click', function () { setMode(false); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !bar.hidden) setMode(false); });
  all.addEventListener('change', function () {
    boxes.forEach(function (b) { b.checked = all.checked; });
    refresh();
  });

  list.addEventListener('click', function (e) {
    if (!list.classList.contains('selecting')) return;
    var row = e.target.closest('.chat-row');
    if (!row) return;
    var box = row.querySelector('.chat-check');
    if (e.target !== box) { e.preventDefault(); box.checked = !box.checked; }
    var i = boxes.indexOf(box);
    if (e.shiftKey && lastIndex >= 0) {
      var a = Math.min(i, lastIndex), z = Math.max(i, lastIndex);
      for (var k = a; k <= z; k++) boxes[k].checked = box.checked;
    }
    lastIndex = i;
    refresh();
  });

  del.addEventListener('click', async function () {
    var ids = ticked().map(function (b) { return b.value; });
    if (!ids.length) return;
    if (!confirm('Delete ' + ids.length + (ids.length === 1 ? ' chat' : ' chats') + '? This cannot be undone.')) return;
    del.disabled = true;
    try {
      var res = await fetch('/ui/chats/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: ids }) });
      if (!res.ok) throw new Error((await res.text()) || res.statusText);
    } catch (err) {
      alert('Could not delete the chats: ' + err.message);
      refresh();
      return;
    }
    var m = location.pathname.match(/^\/chat\/([^/]+)$/);
    location.href = m && ids.indexOf(m[1]) >= 0 ? '/chat' : location.pathname + location.search;
  });
})();
