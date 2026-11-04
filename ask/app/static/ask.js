/* vCommunity Ask chat page: streams answers from /ui/chat (server-sent events),
   renders markdown with the vendored marked + DOMPurify, and shows source cards. */
(function () {
  'use strict';
  var thread = document.getElementById('thread');
  var form = document.getElementById('composer');
  var input = document.getElementById('question');
  var scope = document.getElementById('scope');
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
  }

  function statusBadge(status) {
    if (!status) return null;
    var b = el('span', 'badge ' + (status === 'reviewed' ? 'badge-success' : 'badge-warn'), status);
    b.title = status === 'draft' ? 'Unreviewed content in the knowledge base' : status;
    return b;
  }

  function linkLabel(url) {
    var host = '';
    try { host = new URL(url, location.href).hostname.replace(/^www\./, ''); } catch (e) { /* relative or bad URL */ }
    return host === 'github.com' ? 'View on GitHub ↗' : host ? 'View on ' + host + ' ↗' : 'View source ↗';
  }

  function sourceCard(s, prefix) {
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
        var gh = el('a', null, linkLabel(s.url)); gh.href = s.url; gh.target = '_blank'; gh.rel = 'noopener';
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
    var hits = sources.filter(function (s) { return s.kind !== 'linked'; });
    var linked = sources.filter(function (s) { return s.kind === 'linked'; });
    if (strip) {
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
      if (linked.length) {
        var more = el('a', 'src-chip more', '+' + linked.length + ' linked');
        more.href = '#' + prefix + '-' + linked[0].n;
        more.dataset.card = prefix + '-' + linked[0].n;
        more.title = 'Files the matches link to (same_name, internal_derive, related)';
        strip.appendChild(more);
      }
    }
    var det = el('details', 'sources');
    det.appendChild(el('summary', null, 'Source details (' + hits.length + (linked.length ? ' + ' + linked.length + ' linked' : '') + ')'));
    [[hits, ''], [linked, 'Linked files']].forEach(function (g) {
      if (!g[0].length) return;
      if (g[1]) det.appendChild(el('div', 'src-group', g[1]));
      var grid = el('div', 'src-grid');
      g[0].forEach(function (s) { grid.appendChild(sourceCard(s, prefix)); });
      det.appendChild(grid);
    });
    container.appendChild(det);
  }

  function statsLine(container, m) {
    container.innerHTML = '';
    var parts = [];
    if (m.model) parts.push(m.model);
    var st = m.stats || {};
    if (st.seconds != null) parts.push(st.seconds + ' s');
    if (st.tok_per_s) parts.push(st.tok_per_s + ' tokens/s');
    container.appendChild(el('span', null, parts.join(' · ')));
    var copy = el('button', 'link-btn', 'Copy answer');
    copy.type = 'button';
    copy.onclick = function () {
      try { navigator.clipboard.writeText(m.content || ''); copy.textContent = 'Copied'; } catch (e) {}
      setTimeout(function () { copy.textContent = 'Copy answer'; }, 1500);
    };
    container.appendChild(copy);
  }

  function addUser(text) {
    var row = el('div', 'msg user');
    row.appendChild(el('div', 'bubble', text));
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
  window.addEventListener('scroll', function () {
    if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 80) follow = true;
  }, { passive: true });
  function scrollDown(force) {
    if (force) follow = true;
    if (follow) window.scrollTo(0, document.body.scrollHeight);
  }

  function busy(on) {
    sendBtn.hidden = on; stopBtn.hidden = !on; input.disabled = on;
    if (!on) input.focus();
  }

  function addSidebarChat(id, title) {
    var nav = document.querySelector('.sidenav');
    var empty = nav.querySelector('.nav-empty');
    if (empty) empty.remove();
    var section = Array.prototype.find.call(nav.querySelectorAll('.nav-section'), function (s) { return s.textContent === 'Recent chats'; });
    var a = el('a', 'nav-link sub chat-link active', title);
    a.href = '/chat/' + id; a.title = title;
    nav.querySelectorAll('.nav-link.active').forEach(function (x) { x.classList.remove('active'); });
    section.insertAdjacentElement('afterend', a);
  }

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
    function paint() {
      pending = false;
      renderMarkdown(a.text, content || '', a.prefix);
      if (thinking) a.thinkPre.textContent = thinking;
    }
    try {
      var res = await fetch('/ui/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
        body: JSON.stringify({ chat_id: chatId, question: question, scope: scope.value })
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
              document.getElementById('chat-title').textContent = question.slice(0, 120);
              document.getElementById('delete-chat').hidden = false;
              addSidebarChat(chatId, question.slice(0, 120));
            }
            sourceCards(a.sources, payload.sources, a.prefix, a.strip);
            a.text.innerHTML = '<span class="typing"><i></i><i></i><i></i></span> <span class="subtle">' +
              (payload.sources.length ? 'Found ' + payload.sources.length + ' sources, asking ' : 'No matching sources, asking ') +
              DOMPurify.sanitize(payload.model) + '…</span>';
          } else if (ev === 'thinking') {
            thinking += payload; a.thinking.hidden = false;
            if (!pending) { pending = true; requestAnimationFrame(paint); }
          } else if (ev === 'token') {
            content += payload;
            if (!pending) { pending = true; requestAnimationFrame(paint); }
          } else if (ev === 'done') {
            paint();
            statsLine(a.stats, { model: payload.model, stats: payload.stats, content: content });
          } else if (ev === 'error') {
            if (payload.chat_id && !chatId) chatId = payload.chat_id;
            a.row.classList.add('failed');
            a.text.innerHTML = '';
            a.text.appendChild(el('div', 'alert alert-danger', payload.message));
          }
        }
        scrollDown();
      }
    } catch (e) {
      if (e.name === 'AbortError') {
        paint();
        a.stats.textContent = 'Stopped';
      } else {
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
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  function autosize() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 220) + 'px'; }
  input.addEventListener('input', autosize);
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
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      card.classList.add('flash'); setTimeout(function () { card.classList.remove('flash'); }, 1200);
    }
  });
  document.getElementById('delete-chat').addEventListener('click', async function () {
    if (!chatId || !confirm('Delete this chat?')) return;
    await fetch('/ui/chats/' + chatId + '/delete', { method: 'POST' });
    location.href = '/chat';
  });

  (window.ASK.messages || []).forEach(renderSaved);
  if (window.ASK.messages && window.ASK.messages.length) scrollDown();
  input.focus();
})();
