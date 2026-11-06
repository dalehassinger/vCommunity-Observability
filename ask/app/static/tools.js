/* vCommunity Ask: the MCP tools page. Lists the MCP servers of the host agent with their
   tools, and saves each tool's policy (Allowed, Ask first, Off) and the tool limits. */
(function () {
  'use strict';
  var box = document.getElementById('mcp-servers');
  var note = document.getElementById('mcp-note');
  var msg = document.getElementById('mcp-msg');
  var changed = {};

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function status(cls, text) {
    var s = el('span', 'status ' + cls); s.appendChild(el('i')); s.appendChild(el('span', null, text)); return s;
  }

  function server(s) {
    var card = el('div', 'card mcp-server');
    var head = el('div', 'card-header mcp-head');
    head.appendChild(el('span', null, s.name));
    head.appendChild(s.running ? status('status-green', 'running') : s.starting ? status('status-yellow', 'starting…') : status('status-red', 'not running'));
    var rl = el('button', 'btn btn-outline btn-sm', 'Reload tools');
    rl.type = 'button';
    rl.onclick = async function () {
      rl.disabled = true; rl.textContent = 'Reloading…';
      try { await AskAdmin.post('/ui/mcp/' + encodeURIComponent(s.name) + '/reload'); await load(true); }
      catch (e) { rl.disabled = false; rl.textContent = 'Reload tools'; alert(e.message); }
    };
    head.appendChild(rl);
    card.appendChild(head);
    var b = el('div', 'card-block');
    var info = el('p', 'subtle mcp-cmd');
    info.appendChild(el('code', null, s.command));
    if (s.server && s.server.name) info.appendChild(el('span', null, ' · ' + s.server.name + (s.server.version ? ' ' + s.server.version : '')));
    b.appendChild(info);
    if (s.error) b.appendChild(el('div', 'alert alert-danger', s.error));
    if (s.stderr && s.stderr.length) {
      var d = el('details', 'mcp-stderr');
      d.appendChild(el('summary', null, 'Server messages (stderr)'));
      d.appendChild(el('pre', null, s.stderr.join('\n')));
      if (s.error) d.open = true;
      b.appendChild(d);
    }
    var tools = s.tools || [];
    if (!tools.length) { b.appendChild(el('p', 'subtle', s.starting ? 'Starting the server…' : 'No tools.')); card.appendChild(b); return card; }
    var wrap = el('div', 'table-wrap');
    var t = el('table', 'datagrid mcp-tools');
    t.innerHTML = '<thead><tr><th>Tool</th><th>What it does</th><th>Kind</th><th>In chats</th></tr></thead>';
    var tb = el('tbody');
    tools.forEach(function (tool) {
      var tr = el('tr');
      tr.appendChild(el('td', null)).appendChild(el('code', null, tool.name));
      tr.appendChild(el('td', 'msg', (tool.description || '').trim()));
      var k = el('td');
      k.appendChild(el('span', 'badge ' + (tool.read_only ? 'badge-success' : 'badge-warn'), tool.read_only ? 'read-only' : 'changes things'));
      tr.appendChild(k);
      var sel = el('select', 'mcp-policy');
      [['allow', 'Allowed'], ['ask', 'Ask first'], ['off', 'Off']].forEach(function (o) {
        var op = el('option', null, o[1]); op.value = o[0]; sel.appendChild(op);
      });
      sel.value = tool.policy;
      sel.setAttribute('aria-label', 'Policy for ' + tool.name);
      sel.onchange = function () { changed[s.name + '/' + tool.name] = sel.value; msg.textContent = 'Not saved yet.'; msg.className = 'form-msg'; };
      var c = el('td'); c.appendChild(sel); tr.appendChild(c);
      tb.appendChild(tr);
    });
    t.appendChild(tb); wrap.appendChild(t); b.appendChild(wrap);
    card.appendChild(b);
    return card;
  }

  async function load(fresh) {
    var d;
    try { d = await (await fetch('/ui/mcp' + (fresh ? '?fresh=1' : ''))).json(); }
    catch (e) { box.innerHTML = ''; box.appendChild(el('div', 'alert alert-danger', e.message)); return; }
    document.getElementById('m-rounds').value = d.max_rounds;
    document.getElementById('m-chars').value = d.result_chars;
    var problems = [];
    if (!d.agent_url) problems.push('No Host agent URL (Settings).');
    else if (!d.agent_token_set) problems.push('ASK_AGENT_TOKEN is not set in .env, so the host agent refuses Ask.');
    if (d.error) problems.push(d.error);
    note.hidden = !problems.length;
    note.textContent = problems.join(' ');
    box.innerHTML = '';
    if (!d.servers.length) {
      var e = el('div', 'card');
      var eb = el('div', 'card-block');
      eb.appendChild(el('p', null, 'No MCP servers yet. List them on the Mac in ~/.config/ask-host-agent/mcp.json ' +
        '(the same format as an .mcp.json file), or install the agent with -McpConfig pointing at your .mcp.json. See the README, "MCP tools".'));
      if (d.config) eb.appendChild(el('p', 'subtle', 'The agent reads: ' + d.config));
      e.appendChild(eb); box.appendChild(e);
    }
    d.servers.forEach(function (s) { box.appendChild(server(s)); });
    if (d.servers.some(function (s) { return s.starting; })) setTimeout(function () { load(false); }, 3000);
  }

  document.getElementById('mcp-refresh').onclick = function () { load(true); };
  document.getElementById('mcp-save').onclick = async function () {
    try {
      await AskAdmin.post('/ui/mcp/policy', { policy: changed,
        mcp_max_rounds: document.getElementById('m-rounds').value, mcp_result_chars: document.getElementById('m-chars').value });
      changed = {};
      msg.textContent = 'Saved. Takes effect on the next question.'; msg.className = 'form-msg ok';
    } catch (e) { msg.textContent = 'Not saved: ' + e.message; msg.className = 'form-msg bad'; }
  };
  load(false);
})();
