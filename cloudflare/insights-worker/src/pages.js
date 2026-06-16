// HTML pages served directly by the Worker (same-origin, so no CORS is needed):
//   GET /          → SIGNUP_HTML       (public landing + GitHub sign-in button)
//   GET /console   → CONSOLE_HTML      (operator admin console; token entered client-side)
//   /oauth/...      → resultPage / tokenResultPage / signupDisabledPage
//
// This module is the CANONICAL source for both pages (the old standalone files
// under cloudflare/console/ are superseded).  Everything is plain template
// literals; the only interpolation is ${PAGE_CSS} in the <style> blocks and the
// escaped values in the result pages.  The inline <script> in CONSOLE_HTML uses
// string concatenation only (no backticks / no ${...}) so it survives being
// embedded inside this template literal.
//
// SECURITY: no secret is ever embedded here.  The admin console keeps the
// operator's token in sessionStorage and sends it only to this Worker.  A
// freshly-minted contributor token is injected (escaped) into tokenResultPage()
// exactly once at OAuth-callback time and never stored.

/** Minimal escape for values interpolated into HTML text / attributes. */
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(
    /[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])
  );
}

const PAGE_CSS = `
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; max-width: 980px; margin: 1.5rem auto; padding: 0 1rem; }
h1 { font-size: 1.35rem; } h2 { font-size: 1.05rem; margin-top: 1.6rem; border-bottom: 1px solid #8884; padding-bottom: .25rem; }
fieldset { border: 1px solid #8886; border-radius: 8px; margin: .75rem 0; padding: .75rem 1rem; }
legend { padding: 0 .4rem; color: #888; }
label { display: inline-block; min-width: 10rem; }
input, select, textarea { font: inherit; padding: .3rem .45rem; margin: .15rem 0; border-radius: 6px; border: 1px solid #8886; background: transparent; color: inherit; }
input[type=text], input[type=password], input[type=number] { width: 22rem; max-width: 100%; }
textarea { width: 100%; height: 6rem; font-family: ui-monospace, monospace; }
button { font: inherit; padding: .4rem .9rem; margin: .25rem .25rem .25rem 0; cursor: pointer; border-radius: 6px; border: 1px solid #58f8; background: #58f2; color: inherit; }
button:hover { background: #58f4; }
button.danger { border-color: #e334; background: #e332; }
table { border-collapse: collapse; width: 100%; margin-top: .4rem; font-size: .92em; }
th, td { border-bottom: 1px solid #8883; padding: .3rem .4rem; text-align: left; vertical-align: top; }
pre { background: #8881; padding: .6rem; border-radius: 6px; overflow: auto; max-height: 22rem; white-space: pre-wrap; word-break: break-word; }
code { background: #8882; padding: 0 .3rem; border-radius: 3px; }
.hint { color: #888; font-size: .85em; } .warn { color: #c00; }
.card { border: 1px solid #8886; border-radius: 10px; padding: 1rem 1.25rem; margin: 1rem 0; }
a.btn { display: inline-block; padding: .6rem 1.1rem; border: 1px solid #58f; border-radius: 8px; text-decoration: none; margin: .5rem 0; background: #58f2; color: inherit; }
`;

// ───────────────────────── public signup landing ─────────────────────────
export const SIGNUP_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crucible Run Insights — Join</title>
<style>${PAGE_CSS}</style>
</head>
<body>
<h1>Crucible Run Insights — join the shared corpus</h1>
<p>Crucible runs locally and can mirror its <b>run-insight ledger</b> to a shared
cloud corpus, so contributors accumulate signal together. Sign in with GitHub to
get your contributor token.</p>

<div class="card">
  <h2>What you get</h2>
  <ul>
    <li><b>Write</b> — your local Crucible uploads its run insights to the shared corpus.</li>
    <li><b>Read</b> — you can fetch the corpus <b>aggregates &amp; metadata</b>
      (counts, per-project / mode / outcome stats, index columns).
      Raw run payloads are never exposed.</li>
    <li>You <b>cannot delete</b>. Only the operator can remove records.</li>
  </ul>
  <p class="hint">Uploads are accepted only if they match Crucible's own event
  format, pass a server-side secret scan, and your account is not banned.</p>
</div>

<div class="card">
  <h2>Get your token</h2>
  <a class="btn" href="/oauth/github/start">Sign in with GitHub</a>
  <p class="hint">We read only your public GitHub id + username, for attribution.
  Your token is shown once — paste it into your local <code>.env</code>:</p>
  <pre>CRUCIBLE_RUN_INSIGHTS_BACKEND=dual
CRUCIBLE_RUN_INSIGHTS_API_URL=&lt;this site's URL&gt;
CRUCIBLE_RUN_INSIGHTS_API_TOKEN=&lt;your issued token&gt;</pre>
</div>

<p class="hint">Tokens are individually scoped and revocable; only a hash is
stored server-side. Do not share your token. Never include secrets in your runs.</p>
</body>
</html>`;

// ───────────────────────── operator admin console ─────────────────────────
export const CONSOLE_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Crucible Insights — Admin Console</title>
<style>${PAGE_CSS}</style>
</head>
<body>
<h1>Crucible Insights — Admin Console</h1>
<p class="hint">Private operator tool. For real protection, serve this behind
<b>Cloudflare Access</b> (your identity only). The admin token you paste stays in
<code>sessionStorage</code> (cleared when the tab closes) and is sent only to this Worker.</p>

<fieldset>
  <legend>Connection</legend>
  <div><label for="base">Worker URL</label><input type="text" id="base" placeholder="https://...workers.dev"></div>
  <div><label for="token">Admin token</label><input type="password" id="token" placeholder="crk_... (admin scope)"></div>
  <button onclick="saveCfg()">Save</button>
  <span id="cfgState" class="hint"></span>
</fieldset>

<h2>Overview</h2>
<fieldset>
  <button onclick="loadStats()">Load stats</button>
  <div id="statsOut" class="hint">(counts appear here)</div>
</fieldset>

<h2>Records</h2>
<fieldset>
  <div>
    <label>Filter</label>
    <select id="f_stream"><option value="">stream: any</option><option>output</option><option>error</option><option>debate</option><option>params</option></select>
    <select id="f_trust"><option value="">trust: any</option><option>approved</option><option>staged</option></select>
    <input type="text" id="f_run" placeholder="run_id" style="width:12rem">
    <input type="text" id="f_contrib" placeholder="contributor_id" style="width:12rem">
    <button onclick="loadRecords(true)">Load records</button>
    <span id="recCount" class="hint"></span>
  </div>
  <table>
    <thead><tr>
      <th><input type="checkbox" onclick="toggleAll(this)"></th>
      <th>stream</th><th>trust</th><th>mode</th><th>kind</th>
      <th>project</th><th>contributor</th><th>ts</th><th>content_id</th>
    </tr></thead>
    <tbody id="recBody"></tbody>
  </table>
  <button id="recMore" style="display:none" onclick="loadRecords(false)">Load more</button>
  <button class="danger" onclick="deleteSelected()">Delete selected</button>
</fieldset>

<fieldset>
  <legend>Danger zone — bulk delete</legend>
  <div><label>Delete by run_id</label><input type="text" id="del_run"><button class="danger" onclick="delByRun()">Delete run</button></div>
  <div><label>Delete by contributor_id</label><input type="text" id="del_contrib"><button class="danger" onclick="delByContrib()">Delete all from contributor</button></div>
  <p class="hint">These delete <b>approved</b> rows too. Irreversible.</p>
</fieldset>

<h2>Tokens</h2>
<fieldset>
  <legend>Issue</legend>
  <div><label>Contributor id</label><input type="text" id="i_contrib"></div>
  <div><label>Scope</label>
    <select id="i_scope"><option>contributor</option><option>ingest</option><option>read</option><option>admin</option></select></div>
  <div><label>Label (optional)</label><input type="text" id="i_label"></div>
  <div><label>Daily quota (optional)</label><input type="number" id="i_quota" min="1"></div>
  <button onclick="issueToken()">Issue token</button>
  <p class="warn">The raw token is shown once — copy it now and send it securely.</p>
</fieldset>
<fieldset>
  <legend>List / revoke</legend>
  <button onclick="listTokens()">List tokens</button>
  <label style="min-width:auto">&nbsp; Revoke token id</label>
  <input type="text" id="r_token"><button onclick="revokeToken()">Revoke</button>
</fieldset>

<h2>Curation (staged quarantine)</h2>
<fieldset>
  <button onclick="listStaged()">List staged</button>
  <div><label>Promote content_ids</label><input type="text" id="p_ids" placeholder="comma-separated"></div>
  <div><label>...or by contributor</label><input type="text" id="p_contrib"></div>
  <button onclick="promote()">Promote to approved</button>
  <div><label>Reject content_ids</label><input type="text" id="x_ids" placeholder="comma-separated"></div>
  <button onclick="reject()">Reject (delete staged)</button>
</fieldset>

<h2>Contributors</h2>
<fieldset>
  <button onclick="listContributors()">List contributors</button>
  <div><label>Contributor id</label><input type="text" id="c_id"></div>
  <div><label>Reputation (optional)</label><input type="number" id="c_rep" step="0.01"></div>
  <div><label>Status (optional)</label>
    <select id="c_status"><option value="">(unchanged)</option><option>active</option><option>banned</option></select></div>
  <button onclick="setContributor()">Set</button>
</fieldset>

<h2>Distilled (what read tokens consume)</h2>
<fieldset>
  <div><label>Kind</label><input type="text" id="d_kind" placeholder="avoidance | skills | ..."></div>
  <div><label for="d_payload">Payload (JSON)</label></div>
  <textarea id="d_payload" placeholder='{"items": []}'></textarea>
  <button onclick="publishDistilled()">Publish</button>
</fieldset>

<h2>Raw output</h2>
<pre id="out">(results appear here)</pre>

<script>
  var $ = function(id){ return document.getElementById(id); };
  var out = function(v){ $('out').textContent = typeof v === 'string' ? v : JSON.stringify(v, null, 2); };
  var ESC = { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;' };
  function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, function(c){ return ESC[c]; }); }
  function trunc(s,n){ s = String(s==null?'':s); return s.length>n ? s.slice(0,n)+'...' : s; }

  function loadCfg(){
    $('base').value = sessionStorage.getItem('cf_base') || location.origin;
    $('token').value = sessionStorage.getItem('cf_token') || '';
    $('cfgState').textContent = sessionStorage.getItem('cf_base') ? 'loaded' : 'defaulting to this origin';
  }
  function saveCfg(){
    sessionStorage.setItem('cf_base', $('base').value.trim().replace(/\\/+$/, ''));
    sessionStorage.setItem('cf_token', $('token').value.trim());
    $('cfgState').textContent = 'saved (sessionStorage only)';
  }
  function cfg(){
    var base = sessionStorage.getItem('cf_base') || $('base').value.trim().replace(/\\/+$/, '') || location.origin;
    var token = sessionStorage.getItem('cf_token') || $('token').value.trim();
    return { base: base, token: token };
  }
  async function apiRaw(method, path, body){
    var c = cfg();
    if(!c.token){ out('Enter the admin token and click Save first.'); return null; }
    try{
      var headers = { Authorization: 'Bearer ' + c.token };
      if(body) headers['Content-Type'] = 'application/json';
      var res = await fetch(c.base + path, { method: method, headers: headers, body: body ? JSON.stringify(body) : undefined });
      var data; try{ data = await res.json(); }catch(e){ data = await res.text(); }
      return { status: res.status, data: data };
    }catch(e){ out('Request failed: ' + e); return null; }
  }
  async function api(method, path, body){
    var r = await apiRaw(method, path, body);
    if(r) out({ http_status: r.status, response: r.data });
  }
  var csv = function(s){ return (s||'').split(',').map(function(x){ return x.trim(); }).filter(Boolean); };

  function grpTable(title, rows){
    rows = rows || [];
    var h = '<div style="margin-top:.5rem"><b>' + esc(title) + '</b><table>';
    for(var i=0;i<rows.length;i++){
      h += '<tr><td>' + esc(rows[i].k==null?'(null)':rows[i].k) + '</td><td style="text-align:right">' + esc(rows[i].n) + '</td></tr>';
    }
    return h + '</table></div>';
  }
  async function loadStats(){
    var r = await apiRaw('GET', '/v1/admin/stats');
    if(!r) return;
    if(r.status !== 200){ out({ http_status: r.status, response: r.data }); return; }
    var d = r.data;
    var h = '<b>Total events:</b> ' + esc(d.total) + ' &nbsp;<span class="hint">' + esc(d.first_ts||'-') + ' to ' + esc(d.last_ts||'-') + '</span>';
    h += grpTable('By stream', d.by_stream);
    h += grpTable('By trust_state', d.by_trust_state);
    h += grpTable('By mode', d.by_mode);
    h += grpTable('Top contributors', d.by_contributor);
    $('statsOut').innerHTML = h;
  }

  var recCursor = null;
  function recQuery(reset){
    var p = new URLSearchParams();
    var st = $('f_stream').value; if(st) p.set('stream', st);
    var ts = $('f_trust').value; if(ts) p.set('trust_state', ts);
    var rid = $('f_run').value.trim(); if(rid) p.set('run_id', rid);
    var cid = $('f_contrib').value.trim(); if(cid) p.set('contributor_id', cid);
    p.set('limit', '50');
    if(!reset && recCursor) p.set('cursor', recCursor);
    return '/v1/insights/events?' + p.toString();
  }
  async function loadRecords(reset){
    if(reset){ recCursor = null; $('recBody').innerHTML = ''; }
    var r = await apiRaw('GET', recQuery(reset));
    if(!r) return;
    if(r.status !== 200){ out({ http_status: r.status, response: r.data }); return; }
    var rows = (r.data && r.data.events) || [];
    var h = $('recBody').innerHTML;
    for(var i=0;i<rows.length;i++){
      var e = rows[i];
      h += '<tr>'
        + '<td><input type="checkbox" class="rsel" value="' + esc(e.content_id) + '"></td>'
        + '<td>' + esc(e.stream) + '</td>'
        + '<td>' + esc(e.trust_state) + '</td>'
        + '<td>' + esc(e.mode) + '</td>'
        + '<td>' + esc(e.kind) + '</td>'
        + '<td title="' + esc(e.project_name) + '">' + esc(trunc(e.project_name,24)) + '</td>'
        + '<td>' + esc(e.contributor_id==null?'-':e.contributor_id) + '</td>'
        + '<td class="hint">' + esc(e.ts) + '</td>'
        + '<td class="hint" title="' + esc(e.content_id) + '">' + esc(trunc(e.content_id,20)) + '</td>'
        + '</tr>';
    }
    $('recBody').innerHTML = h;
    recCursor = (r.data && r.data.next_cursor) || null;
    $('recMore').style.display = recCursor ? 'inline-block' : 'none';
    $('recCount').textContent = document.querySelectorAll('#recBody tr').length + ' loaded';
  }
  function toggleAll(box){ var els = document.querySelectorAll('.rsel'); for(var i=0;i<els.length;i++){ els[i].checked = box.checked; } }
  function selectedIds(){ var els = document.querySelectorAll('.rsel:checked'); var ids = []; for(var i=0;i<els.length;i++){ ids.push(els[i].value); } return ids; }
  async function deleteSelected(){
    var ids = selectedIds();
    if(!ids.length){ out('No rows selected.'); return; }
    if(!confirm('Permanently delete ' + ids.length + ' event(s)? This cannot be undone.')) return;
    var r = await apiRaw('POST', '/v1/admin/events/delete', { content_ids: ids });
    if(!r) return;
    out({ http_status: r.status, response: r.data });
    await loadRecords(true); loadStats();
  }
  async function delByRun(){
    var rid = $('del_run').value.trim();
    if(!rid){ out('Enter a run_id.'); return; }
    if(!confirm('Delete ALL events for run_id ' + rid + '? This cannot be undone.')) return;
    var r = await apiRaw('POST', '/v1/admin/events/delete', { run_id: rid });
    if(r) out({ http_status: r.status, response: r.data });
    loadStats();
  }
  async function delByContrib(){
    var cid = $('del_contrib').value.trim();
    if(!cid){ out('Enter a contributor_id.'); return; }
    if(!confirm('Delete ALL events from contributor ' + cid + '? This cannot be undone.')) return;
    var r = await apiRaw('POST', '/v1/admin/events/delete', { contributor_id: cid });
    if(r) out({ http_status: r.status, response: r.data });
    loadStats();
  }

  function issueToken(){
    var body = { contributor_id: $('i_contrib').value.trim(), scope: $('i_scope').value };
    if($('i_label').value.trim()) body.label = $('i_label').value.trim();
    if($('i_quota').value) body.daily_quota = parseInt($('i_quota').value, 10);
    api('POST', '/v1/admin/tokens', body);
  }
  var listTokens = function(){ return api('GET', '/v1/admin/tokens'); };
  function revokeToken(){ var id = $('r_token').value.trim(); if(id) api('POST', '/v1/admin/tokens/' + encodeURIComponent(id) + '/revoke'); }
  var listStaged = function(){ return api('GET', '/v1/admin/staged?limit=100'); };
  function promote(){
    var body = {};
    var ids = csv($('p_ids').value); if(ids.length) body.content_ids = ids;
    if($('p_contrib').value.trim()) body.contributor_id = $('p_contrib').value.trim();
    api('POST', '/v1/admin/events/promote', body);
  }
  function reject(){ var ids = csv($('x_ids').value); if(ids.length) api('POST', '/v1/admin/events/reject', { content_ids: ids }); }
  var listContributors = function(){ return api('GET', '/v1/admin/contributors'); };
  function setContributor(){
    var id = $('c_id').value.trim(); if(!id) return;
    var body = {};
    if($('c_rep').value) body.reputation = parseFloat($('c_rep').value);
    if($('c_status').value) body.status = $('c_status').value;
    api('POST', '/v1/admin/contributors/' + encodeURIComponent(id), body);
  }
  function publishDistilled(){
    var payload; try{ payload = JSON.parse($('d_payload').value); }catch(e){ out('Payload must be valid JSON'); return; }
    api('POST', '/v1/admin/distilled', { kind: $('d_kind').value.trim(), payload: payload });
  }
  loadCfg();
</script>
</body>
</html>`;

// ───────────────────────── OAuth result / error pages ─────────────────────────

/** Shown when GitHub OAuth secrets are not configured on the deployment. */
export function signupDisabledPage() {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signup not available</title>
<style>${PAGE_CSS}</style>
</head>
<body>
<h1>Signup is not configured yet</h1>
<p>GitHub sign-in has not been enabled on this deployment. The operator needs to
set the <code>GITHUB_OAUTH_CLIENT_ID</code> and <code>GITHUB_OAUTH_CLIENT_SECRET</code>
Worker secrets.</p>
<p class="hint">If you are the operator, see OPENING_UP.md → "GitHub OAuth setup".</p>
</body>
</html>`;
}

/** Generic titled message page (sign-in failures, denials, etc.). */
export function resultPage(title, msg) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>${PAGE_CSS}</style>
</head>
<body>
<h1>${escapeHtml(title)}</h1>
<p>${escapeHtml(msg)}</p>
<p><a class="btn" href="/">Back to signup</a></p>
</body>
</html>`;
}

/** Success page after OAuth: shows the freshly-issued token exactly once. */
export function tokenResultPage(token, origin, login) {
  const t = escapeHtml(token);
  const o = escapeHtml(origin);
  const who = escapeHtml(login);
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your Crucible token</title>
<style>${PAGE_CSS}</style>
</head>
<body>
<h1>Welcome, ${who}</h1>
<p class="warn"><b>Copy your token now — it is shown only once.</b> If you lose it,
sign in again to get a fresh one (the old one is revoked).</p>
<div class="card">
  <h2>Your contributor token</h2>
  <pre id="tok">${t}</pre>
  <button onclick="navigator.clipboard.writeText(document.getElementById('tok').textContent)">Copy token</button>
</div>
<div class="card">
  <h2>Add to your local .env</h2>
  <pre>CRUCIBLE_RUN_INSIGHTS_BACKEND=dual
CRUCIBLE_RUN_INSIGHTS_API_URL=${o}
CRUCIBLE_RUN_INSIGHTS_API_TOKEN=${t}</pre>
</div>
<p class="hint">Scope: contributor (write + read aggregates/metadata). You cannot
read raw payloads or delete records.</p>
</body>
</html>`;
}
