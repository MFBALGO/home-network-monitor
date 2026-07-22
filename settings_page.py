#!/usr/bin/env python3
"""
Home Network Monitor - the setup wizard and settings pages.

Two self-contained HTML pages kept as Python strings and served from
memory by serve.py, ONLY to localhost (the LAN never sees them, so the
"config files unreachable from the network" guarantee holds). They are
deliberately not part of dashboard.html - that file is regenerated every
60 seconds by another process, which would wipe an open form.

Both pages talk to the localhost-only /api/* endpoints (settings_api.py):
    GET  /api/config           read all three config files
    POST /api/config           validated, atomic save
    POST /api/discover         start a LAN scan
    GET  /api/discover/status  poll it

Styling reuses the dashboard's dark "Mission Control" palette so the
pages feel like one product.
"""

_SHARED_HEAD = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__PAGE_TITLE__</title>
<style>
  :root {
    --page: #05080f;
    --surface-1: #0c121d;
    --surface-2: #101a2b;
    --text-primary: #e8eef8;
    --text-secondary: #a2b3cb;
    --muted: #5e7290;
    --border: rgba(130,170,230,0.15);
    --border-soft: rgba(130,170,230,0.07);
    --accent: #3fc6ff;
    --accent-soft: rgba(63,198,255,0.09);
    --status-good: #0ca30c;
    --status-good-bg: rgba(12,163,12,0.14);
    --status-warning: #fab219;
    --status-warning-bg: rgba(250,178,25,0.12);
    --status-critical: #e66767;
    --status-critical-bg: rgba(230,103,103,0.14);
    --font-mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    color-scheme: dark;
  }
  * { box-sizing: border-box; scrollbar-width: none !important; -ms-overflow-style: none !important; }
  *::-webkit-scrollbar { display: none !important; width: 0 !important; height: 0 !important; }
  html, body { overflow-x: hidden; }
  body { margin: 0; background: var(--page); color: var(--text-primary);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 28px 18px 60px; }
  h1 { font-size: 22px; margin: 0 0 4px; font-weight: 650; letter-spacing: -0.01em; }
  h2 { font-size: 16px; margin: 26px 0 10px; color: var(--text-primary); font-weight: 650; }
  h3 { font-size: 14px; font-weight: 600; color: var(--text-primary); }
  .sub { color: var(--muted); font-size: 13px; margin-bottom: 22px; }
  .sub a { color: var(--accent); text-decoration: none; }
  .card { background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 12px; padding: 18px 20px; margin-bottom: 16px; }
  label { display: block; font-size: 12px; color: var(--text-secondary); margin: 12px 0 4px; }
  input[type=text], input[type=number], input[type=time], select {
    width: 100%; padding: 7px 10px; border-radius: 8px; font-size: 13.5px;
    background: var(--surface-2); color: var(--text-primary);
    border: 1px solid var(--border); outline: none; font-family: inherit; }
  input:focus, select:focus { border-color: var(--accent); }
  input[type=checkbox], input[type=radio] { accent-color: var(--accent); }
  input.invalid { border-color: var(--status-critical); }
  button { padding: 8px 16px; border-radius: 8px; font-size: 13.5px; cursor: pointer;
    background: var(--surface-2); color: var(--text-primary);
    border: 1px solid var(--border); font-family: inherit; }
  button:hover { border-color: var(--accent); }
  button.primary { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); font-weight: 600; }
  button.small { padding: 3px 10px; font-size: 12px; }
  button.danger:hover { border-color: var(--status-critical); color: var(--status-critical); }
  button:disabled { opacity: 0.45; cursor: default; }
  button:disabled:hover { border-color: var(--border); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--muted); font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.4px; padding: 6px 8px; border-bottom: 1px solid var(--border); }
  td { padding: 6px 8px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; }
  td input[type=text], td select { min-width: 90px; }
  .mono { font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary); }
  .badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 10.5px;
    font-family: var(--font-mono); letter-spacing: 0.4px; white-space: nowrap; }
  .badge.gateway { color: var(--accent); border: 1px solid var(--accent); }
  .badge.known { color: var(--status-good); border: 1px solid var(--status-good); }
  .msg { border-radius: 8px; padding: 10px 14px; margin: 12px 0; font-size: 13px; display: none; }
  .msg.error { display: block; background: var(--status-critical-bg); border: 1px solid var(--status-critical); }
  .msg.ok { display: block; background: var(--status-good-bg); border: 1px solid var(--status-good); }
  .msg.warn { display: block; background: var(--status-warning-bg); border: 1px solid var(--status-warning); }
  .msg ul { margin: 6px 0 0; padding-left: 18px; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .grow { flex: 1; }
  .progress-track { height: 8px; border-radius: 999px; background: var(--surface-2);
    border: 1px solid var(--border); overflow: hidden; margin: 14px 0 8px; }
  .progress-fill { height: 100%; width: 0%; background: var(--accent); transition: width 0.4s; }
  .steps { display: flex; gap: 6px; margin-bottom: 20px; font-size: 11.5px; font-weight: 600; }
  .steps span { flex: 1; text-align: center; padding: 5px 2px; border-radius: 6px;
    background: var(--surface-1); border: 1px solid var(--border-soft); color: var(--muted); }
  .steps span.active { border-color: var(--accent); color: var(--accent); }
  .steps span.done { color: var(--status-good); border-color: var(--border); }
  details { margin-top: 14px; }
  summary { cursor: pointer; color: var(--text-secondary); font-size: 13px; }
  /* folded "More…" explainers under a one-line sub — keeps the cards
     scannable without losing the detail for first-time setup */
  details.hint { margin: 0 0 12px; }
  details.hint summary { font-size: 12px; color: var(--muted); }
  details.hint .sub { margin: 6px 0 0; }
  .footer { margin-top: 30px; color: var(--muted); font-size: 11.5px; font-family: var(--font-mono); text-align: center; }
  .tabs { display: flex; gap: 8px; margin-bottom: 18px; }
  .tabs button { border-radius: 8px 8px 0 0; border-bottom: 2px solid transparent; position: relative; }
  .tabs button.active { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
  /* amber dot = this tab has unsaved edits */
  .tabs button.dirty::after { content: ""; position: absolute; top: 6px; right: 6px;
    width: 6px; height: 6px; border-radius: 50%; background: var(--status-warning); }
  /* Floors editor: a mini cross-section of the house. Rows above the
     street-level divider get a sky tint, rows below an earth tint — the
     same visual language as the dashboard's house map. */
  .floor-row { display: flex; gap: 8px; align-items: center; padding: 8px 10px; margin: 4px 0;
    border: 1px solid var(--border-soft); border-radius: 8px; }
  .floor-row.above { background: linear-gradient(180deg, rgba(63,198,255,0.07), rgba(63,198,255,0.02)); }
  .floor-row.below { background: rgba(153,122,81,0.12); border-color: rgba(153,122,81,0.30); }
  .ground-divider { display: flex; align-items: center; gap: 8px; margin: 6px 0; }
  .ground-line { flex: 1; border-top: 2px dashed #79b768; opacity: 0.75; height: 0; }
  .ground-label { color: #79b768; font-family: var(--font-mono); font-size: 10.5px;
    letter-spacing: 1.2px; text-transform: uppercase; white-space: nowrap; }
  .floor-main { white-space: nowrap; }
  .floor-main.active { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
  /* Devices tab: context sub-line under each name (mac · ip · last seen)
     and the divider before named-but-vanished entries */
  .d-ctx { font-family: var(--font-mono); font-size: 11px; color: var(--muted); margin-top: 3px; }
  .d-ctx .on { color: var(--status-good); font-weight: 600; }
  .d-row.d-cleared .d-name { opacity: 0.5; }
  .d-forgot { color: var(--status-warning); font-weight: 600; }
  .d-group td { padding-top: 14px; color: var(--muted); font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.4px; border-bottom: none; font-weight: 600; }
  .thresh-grid { display: grid; grid-template-columns: 110px 1fr 1fr; gap: 8px 12px; align-items: center; }
  .iv-grid { display: grid; grid-template-columns: minmax(180px, 1fr) 150px; gap: 8px 12px; align-items: center; }
  .thresh-grid .hdr { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; }
</style>
</head><body><div class="wrap">
"""

_SHARED_JS = """
function esc(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  };
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch (e) {}
  return {status: resp.status, data};
}
function showMsg(el, kind, text, items) {
  el.className = 'msg ' + kind;
  el.innerHTML = esc(text) + (items && items.length
    ? '<ul>' + items.map(i => '<li>' + esc(i) + '</li>').join('') + '</ul>' : '');
}
function clearMsg(el) { el.className = 'msg'; el.innerHTML = ''; }
function fmtIssue(i) {
  return (i.file ? i.file + (i.path ? ' ' + i.path : '') + ': ' : '') + i.msg;
}
// Reusable floors editor (wizard step 2 + settings General tab): a visual
// stack of floors, top floor first, with a movable dashed "street level"
// line — floors below it are drawn underground (behind earth) on the house
// map. state: {floors: [names], groundIndex: how many floors are above
// street level, main: floor name of the main router, onchange}.
function clampGround(state) {
  // at least the top floor stays above ground; divider can sit at the very
  // bottom (= nothing underground)
  state.groundIndex = Math.min(Math.max(state.groundIndex, 1), state.floors.length || 1);
}
// Convert a config's underground_floors list to a divider position: count
// how many floors, from the bottom up, are marked underground.
function groundIndexFrom(floors, underground) {
  const ug = new Set(underground || []);
  let k = 0;
  for (let i = floors.length - 1; i >= 0 && ug.has(floors[i]); i--) k++;
  return floors.length - k;
}
function renderFloors(state, mountId) {
  const mount = document.getElementById(mountId);
  clampGround(state);
  const divider =
    '<div class="ground-divider"><span class="ground-line"></span>' +
    '<span class="ground-label">street level</span>' +
    '<button class="small" data-role="ground-up" title="Raise the line: one more floor becomes underground">&#8593;</button>' +
    '<button class="small" data-role="ground-down" title="Lower the line: one less floor underground">&#8595;</button>' +
    '<span class="ground-line"></span></div>';
  let html = '';
  state.floors.forEach((f, i) => {
    if (i === state.groundIndex) html += divider;
    const below = i >= state.groundIndex;
    html += '<div class="floor-row ' + (below ? 'below' : 'above') + '">' +
      '<button class="small" data-i="' + i + '" data-role="up" title="Move floor up">&#8593;</button>' +
      '<button class="small" data-i="' + i + '" data-role="down" title="Move floor down">&#8595;</button>' +
      '<input type="text" class="grow" value="' + esc(f) + '" data-i="' + i + '" data-role="name">' +
      '<button class="small floor-main' + (state.main === f ? ' active' : '') + '" data-i="' + i +
        '" data-role="main" title="Draw the main router (your internet gateway) on this floor">' +
        (state.main === f ? '&#9679; main router' : 'main router') + '</button>' +
      '<button class="small danger" data-i="' + i + '" data-role="del" title="Remove floor">&#10005;</button>' +
      '</div>';
  });
  if (state.groundIndex >= state.floors.length) html += divider;
  mount.innerHTML = html;

  const rerender = () => { renderFloors(state, mountId); state.onchange && state.onchange(); };
  mount.onclick = (e) => {
    const t = e.target.closest('button'); if (!t) return;
    const i = +t.dataset.i;
    if (t.dataset.role === 'ground-up') { state.groundIndex--; rerender(); }
    if (t.dataset.role === 'ground-down') { state.groundIndex++; rerender(); }
    if (t.dataset.role === 'del') {
      const removed = state.floors.splice(i, 1)[0];
      if (i < state.groundIndex) state.groundIndex--;
      if (state.main === removed) state.main = state.floors[0] || null;
      rerender();
    }
    if (t.dataset.role === 'up' && i > 0) {
      [state.floors[i-1], state.floors[i]] = [state.floors[i], state.floors[i-1]];
      rerender();
    }
    if (t.dataset.role === 'down' && i < state.floors.length - 1) {
      [state.floors[i+1], state.floors[i]] = [state.floors[i], state.floors[i+1]];
      rerender();
    }
    if (t.dataset.role === 'main') { state.main = state.floors[i]; rerender(); }
  };
  mount.onchange = (e) => {
    const t = e.target; const i = +t.dataset.i;
    if (t.dataset.role === 'name') {
      const old = state.floors[i]; const nu = t.value.trim();
      state.floors[i] = nu;
      if (state.main === old) state.main = nu;
      state.onchange && state.onchange();
    }
  };
}
function addFloor(state, mountId) {
  // new floors go on top (houses grow upward); the underground count is
  // unchanged, so groundIndex shifts with the insert
  state.floors.unshift('New Floor');
  state.groundIndex++;
  if (!state.main) state.main = state.floors[0];
  renderFloors(state, mountId);
  state.onchange && state.onchange();
}
function addBasement(state, mountId) {
  // appended at the bottom, below the divider, so it's underground already
  let name = 'Basement', n = 2;
  while (state.floors.includes(name)) name = 'Basement ' + n++;
  state.floors.push(name);
  if (!state.main) state.main = state.floors[0];
  renderFloors(state, mountId);
  state.onchange && state.onchange();
}
"""

WIZARD_HTML = (_SHARED_HEAD.replace("__PAGE_TITLE__", "Setup — Home Network Monitor") + """
<h1>Set up your network monitor</h1>
<div class="sub">Runs entirely on this machine — nothing here leaves your network.</div>
<div class="steps" id="steps">
  <span data-step="1">1 · SCAN</span><span data-step="2">2 · FLOORS</span>
  <span data-step="3">3 · ROUTERS</span><span data-step="4">4 · FINISH</span>
</div>

<div class="card" id="step1">
  <h2 style="margin-top:0">Scanning your network</h2>
  <div id="scanPhase" class="mono">Starting scan…</div>
  <div class="progress-track"><div class="progress-fill" id="scanBar"></div></div>
  <div class="mono" id="scanCount"></div>
  <div class="msg" id="scanMsg"></div>
  <div class="row" style="margin-top:10px"><button id="scanRetry" style="display:none" class="primary">Retry scan</button></div>
</div>

<div class="card" id="step2" style="display:none">
  <h2 style="margin-top:0">Your house</h2>
  <label>Dashboard title</label>
  <input type="text" id="wTitle" value="Home Network Monitor" maxlength="100">
  <label>Floors — this is your house from the side, exactly as the dashboard will draw it
  (top floor first). Floors under the dashed <span style="color:#79b768">street level</span> line
  are drawn below ground.</label>
  <div id="wFloors"></div>
  <div class="row" style="margin-top:8px">
    <button class="small" id="wAddFloor">+ Add floor on top</button>
    <button class="small" id="wAddBasement">+ Add basement</button>
  </div>
  <div class="row" style="margin-top:16px; justify-content:flex-end">
    <button class="primary" id="toStep3">Continue</button>
  </div>
</div>

<div class="card" id="step3" style="display:none">
  <h2 style="margin-top:0">Routers &amp; access points to monitor</h2>
  <div class="sub" style="margin-bottom:10px">Ticked rows go into monitoring. Your main router
  (gateway) is watched automatically and isn't listed as an extra entry.</div>
  <div id="wRouters"></div>
  <div id="wPingOnly"></div>
  <div class="row" style="margin-top:16px; justify-content:space-between">
    <button id="backTo2">Back</button>
    <button class="primary" id="toStep4">Continue</button>
  </div>
</div>

<div class="card" id="step4" style="display:none">
  <h2 style="margin-top:0">Review &amp; finish</h2>
  <div id="wSummary"></div>
  <details id="wNamesWrap">
    <summary>Name these devices on the dashboard (optional)</summary>
    <div id="wNames" style="margin-top:8px"></div>
  </details>
  <div class="msg" id="saveMsg"></div>
  <div class="row" style="margin-top:16px; justify-content:space-between">
    <button id="backTo3">Back</button>
    <span>
      <button id="wOverwrite" class="danger" style="display:none">Overwrite existing setup</button>
      <button class="primary" id="wSave">Save &amp; start monitoring</button>
    </span>
  </div>
</div>

<div class="card" id="stepDone" style="display:none">
  <h2 style="margin-top:0">You're done</h2>
  <p>The monitor picks up your routers within 15 seconds, and the dashboard
  fills in over the next few minutes as data arrives.</p>
  <p><a href="/" style="color:var(--accent)">Open the dashboard &rarr;</a></p>
</div>

<div class="footer">Home Network Monitor setup — you can rerun this any time at
http://localhost:8080/setup, or fine-tune later in <a href="/settings" style="color:var(--accent)">Settings</a>.</div>

<script>
""" + _SHARED_JS + """
const S = { results: [], floors: null };

function setStep(n) {
  for (const el of document.querySelectorAll('#steps span')) {
    const s = +el.dataset.step;
    el.className = s === n ? 'active' : (s < n ? 'done' : '');
  }
  for (const id of ['step1','step2','step3','step4']) {
    document.getElementById(id).style.display = (+id.slice(4) === n) ? '' : 'none';
  }
  document.getElementById('stepDone').style.display = 'none';
}

// ---- step 1: discovery (auto-starts) ----
async function startScan() {
  document.getElementById('scanRetry').style.display = 'none';
  clearMsg(document.getElementById('scanMsg'));
  const {status} = await api('/api/discover', {});
  if (status !== 202 && status !== 409) {
    showMsg(document.getElementById('scanMsg'), 'error', 'Could not start the scan (HTTP ' + status + ').');
    document.getElementById('scanRetry').style.display = '';
    return;
  }
  poll();
}
const PHASE_LABEL = {
  'starting': 'Starting…',
  'port-scan': 'Checking for router admin pages',
  'ping-sweep': 'Pinging every address (finds mesh nodes with no admin page)',
  'identify': 'Identifying what each device is',
};
async function poll() {
  const {status, data} = await api('/api/discover/status');
  if (status !== 200 || !data) { setTimeout(poll, 2000); return; }
  if (data.state === 'running') {
    document.getElementById('scanPhase').textContent = PHASE_LABEL[data.phase] || data.phase || '…';
    const pct = data.total ? Math.round(100 * data.done / data.total) : 0;
    document.getElementById('scanBar').style.width = pct + '%';
    document.getElementById('scanCount').textContent = data.total ? (data.done + ' / ' + data.total) : '';
    setTimeout(poll, 2000);
  } else if (data.state === 'done') {
    S.results = data.results || [];
    document.getElementById('scanBar').style.width = '100%';
    // double-NAT heads-up from the piggybacked traceroute check
    const topo = data.topology;
    if (topo && topo.double_nat) {
      showMsg(document.getElementById('scanMsg'), 'error',
        'Heads-up: two routers on this network are each doing NAT (double NAT). This can ' +
        'break game consoles, VoIP and port forwarding. Fix: put the ISP box in bridge/modem ' +
        'mode, or set its DMZ to your main router (which mostly mitigates it). Monitoring ' +
        'works fine either way — continue below.');
    }
    buildRouterTable();
    setStep(2);
  } else if (data.state === 'error') {
    showMsg(document.getElementById('scanMsg'), 'error',
      'Scan failed: ' + (data.error || 'unknown') + '. Are you connected to your home network?');
    document.getElementById('scanRetry').style.display = '';
  } else { // idle - server restarted mid-scan
    startScan();
  }
}
document.getElementById('scanRetry').onclick = startScan;

// ---- step 2: floors ----
S.floorState = { floors: ['Ground Floor'], groundIndex: 1, main: 'Ground Floor',
                 onchange: () => refreshFloorSelects() };
renderFloors(S.floorState, 'wFloors');
document.getElementById('wAddFloor').onclick = () => addFloor(S.floorState, 'wFloors');
document.getElementById('wAddBasement').onclick = () => addBasement(S.floorState, 'wFloors');
document.getElementById('toStep3').onclick = () => {
  const fs = S.floorState.floors.map(f => f.trim()).filter(Boolean);
  if (!fs.length) { alert('Add at least one floor.'); return; }
  buildRouterTable();
  setStep(3);
};

// ---- step 3: routers ----
function floorOptions(sel) {
  return S.floorState.floors.map(f =>
    '<option' + (f === sel ? ' selected' : '') + '>' + esc(f) + '</option>').join('');
}
function defaultName(r) {
  return r.title || r.hostname || ('Router at ' + r.ip);
}
function buildRouterTable() {
  const webHits = S.results.filter(r => !r.ping_only);
  const pingOnly = S.results.filter(r => r.ping_only);
  const defFloor = S.floorState.main || S.floorState.floors[0] || '';
  let html = '<table><tr><th></th><th>Name</th><th>Floor</th><th>Details</th></tr>';
  webHits.forEach((r, i) => {
    const idx = S.results.indexOf(r);
    const gw = r.is_gateway;
    html += '<tr>' +
      '<td><input type="checkbox" data-idx="' + idx + '"' +
        (gw ? ' disabled' : (r.suggested || r.known_router_name ? ' checked' : '')) + '></td>' +
      '<td><input type="text" data-name="' + idx + '" value="' +
        esc(r.known_router_name || defaultName(r)) + '"' + (gw ? ' disabled' : '') + '></td>' +
      '<td><select data-floor="' + idx + '"' + (gw ? ' disabled' : '') + '>' + floorOptions(defFloor) + '</select></td>' +
      '<td class="mono">' + esc(r.ip) + (r.mac ? ' · ' + esc(r.mac) : '') +
        (gw ? ' <span class="badge gateway">main gateway — monitored automatically</span>' : '') +
        (r.known_router_name ? ' <span class="badge known">already in routers.json</span>' : '') +
        (r.title || r.server ? '<br>' + esc(r.title || r.server) : '') +
      '</td></tr>';
  });
  html += '</table>';
  if (!webHits.length) html = '<p class="mono">No devices with an admin page found.</p>';
  document.getElementById('wRouters').innerHTML = html;

  if (pingOnly.length) {
    let ph = '<details><summary>Show ' + pingOnly.length + ' more device(s) with no admin page ' +
      '(phones and TVs live here — but so do mesh satellite nodes)</summary>' +
      '<table style="margin-top:8px"><tr><th></th><th>Name</th><th>Floor</th><th>Details</th></tr>';
    pingOnly.forEach((r) => {
      const idx = S.results.indexOf(r);
      ph += '<tr>' +
        '<td><input type="checkbox" data-idx="' + idx + '"' + (r.known_router_name ? ' checked' : '') + '></td>' +
        '<td><input type="text" data-name="' + idx + '" value="' + esc(r.known_router_name || defaultName(r)) + '"></td>' +
        '<td><select data-floor="' + idx + '">' + floorOptions(defFloor) + '</select></td>' +
        '<td class="mono">' + esc(r.ip) + (r.mac ? ' · ' + esc(r.mac) : '') +
          (r.hostname ? ' · ' + esc(r.hostname) : '') +
          (r.known_router_name ? ' <span class="badge known">already in routers.json</span>' : '') +
        '</td></tr>';
    });
    ph += '</table></details>';
    document.getElementById('wPingOnly').innerHTML = ph;
  } else {
    document.getElementById('wPingOnly').innerHTML = '';
  }
}
function refreshFloorSelects() {
  for (const sel of document.querySelectorAll('select[data-floor]')) {
    const cur = sel.value;
    sel.innerHTML = floorOptions(S.floorState.floors.includes(cur) ? cur : (S.floorState.main || ''));
  }
}
function chosenRouters() {
  const out = [];
  for (const cb of document.querySelectorAll('input[type=checkbox][data-idx]')) {
    if (!cb.checked || cb.disabled) continue;
    const idx = +cb.dataset.idx;
    const r = S.results[idx];
    out.push({
      name: (document.querySelector('input[data-name="' + idx + '"]').value || defaultName(r)).trim(),
      ip: r.ip,
      floor: document.querySelector('select[data-floor="' + idx + '"]').value,
      mac: r.mac,
    });
  }
  return out;
}
document.getElementById('backTo2').onclick = () => setStep(2);
document.getElementById('toStep4').onclick = () => { buildReview(); setStep(4); };

// ---- step 4: review + save ----
function buildReview() {
  const routers = chosenRouters();
  const fs = S.floorState.floors;
  document.getElementById('wSummary').innerHTML =
    '<p><b>' + esc(document.getElementById('wTitle').value || 'Home Network Monitor') + '</b><br>' +
    esc(fs.length + ' floor(s): ' + fs.join(', ')) + '<br>' +
    esc(routers.length + ' router(s)/AP(s) to monitor: ' + routers.map(r => r.name).join(', ')) + '</p>';
  const withMac = routers.filter(r => r.mac);
  document.getElementById('wNamesWrap').style.display = withMac.length ? '' : 'none';
  document.getElementById('wNames').innerHTML = withMac.map(r =>
    '<div class="row" style="margin-bottom:6px"><span class="mono" style="width:170px">' + esc(r.mac) + '</span>' +
    '<input type="text" class="grow" data-devname="' + esc(r.mac) + '" value="' + esc(r.name) + '"></div>'
  ).join('');
}
function wizardPayload(overwrite) {
  const routers = chosenRouters().map(r => ({name: r.name, ip: r.ip, floor: r.floor}));
  const devices = {};
  for (const inp of document.querySelectorAll('input[data-devname]')) {
    if (inp.value.trim()) devices[inp.dataset.devname] = inp.value.trim();
  }
  const all = S.floorState.floors.map(f => f.trim());
  const fs = all.filter(Boolean);
  const cfg = {
    title: document.getElementById('wTitle').value.trim() || 'Home Network Monitor',
    floors: fs,
    underground_floors: all.slice(S.floorState.groundIndex).filter(Boolean),
    main_router_floor: fs.includes(S.floorState.main) ? S.floorState.main : fs[fs.length - 1],
  };
  const body = {config: cfg, routers, devices};
  if (overwrite) body.overwrite = true;
  return body;
}
async function saveWizard(overwrite) {
  const msg = document.getElementById('saveMsg');
  clearMsg(msg);
  const {status, data} = await api('/api/config', wizardPayload(overwrite));
  if (status === 200) {
    document.getElementById('step4').style.display = 'none';
    document.getElementById('stepDone').style.display = '';
    for (const el of document.querySelectorAll('#steps span')) el.className = 'done';
  } else if (status === 409) {
    showMsg(msg, 'warn', 'This install is already configured — saving would replace its ' +
      'current router list. Use Settings for small changes, or overwrite everything:');
    document.getElementById('wOverwrite').style.display = '';
  } else {
    showMsg(msg, 'error', 'Not saved — please fix:', (data && data.errors || []).map(fmtIssue));
  }
}
document.getElementById('backTo3').onclick = () => setStep(3);
document.getElementById('wSave').onclick = () => saveWizard(false);
document.getElementById('wOverwrite').onclick = () => saveWizard(true);

setStep(1);
startScan();
</script>
</div></body></html>
""")

SETTINGS_HTML = (_SHARED_HEAD.replace("__PAGE_TITLE__", "Settings — Home Network Monitor") + """
<h1>Settings</h1>
<div class="sub"><a href="/">&larr; back to dashboard</a> &nbsp;·&nbsp; editable only on this machine
&nbsp;·&nbsp; <a href="/setup">rerun the setup wizard</a></div>

<div class="tabs" role="tablist" aria-label="Settings sections">
  <button data-tab="general" class="active" id="tabbtn-general" role="tab" aria-controls="tab-general" aria-selected="true">General</button>
  <button data-tab="routers" id="tabbtn-routers" role="tab" aria-controls="tab-routers" aria-selected="false" tabindex="-1">Routers</button>
  <button data-tab="devices" id="tabbtn-devices" role="tab" aria-controls="tab-devices" aria-selected="false" tabindex="-1">Devices</button>
  <button data-tab="alerts" id="tabbtn-alerts" role="tab" aria-controls="tab-alerts" aria-selected="false" tabindex="-1">Alerts</button>
</div>

<div id="tab-general" role="tabpanel" aria-labelledby="tabbtn-general" tabindex="0">
  <div class="card">
    <label>Dashboard title</label>
    <input type="text" id="gTitle" maxlength="100">
    <label>Floors — your house from the side, exactly as the dashboard's map draws it
    (top floor first). Floors under the dashed <span style="color:#79b768">street level</span> line
    are drawn below ground.</label>
    <div id="gFloors"></div>
    <div class="row" style="margin-top:8px">
      <button class="small" id="gAddFloor">+ Add floor on top</button>
      <button class="small" id="gAddBasement">+ Add basement</button>
    </div>
    <label>Internet plan speeds, Mbps (drawn on the speed chart — leave empty to skip)</label>
    <div class="row">
      <input type="number" id="gPlanDown" placeholder="download" min="1" style="width:130px">
      <input type="number" id="gPlanUp" placeholder="upload" min="1" style="width:130px">
    </div>
    <label>Where this monitor runs — the router/AP the monitor PC is connected to.
    Speed tests and latency measure <b>that path</b>, so the dashboard labels them with it
    (a weak reading may be an in-house link, not the ISP).</label>
    <select id="gMonLoc" style="max-width:280px"><option value="">(not set)</option></select>
    <label>Default chart time range — what every dashboard chart shows when the page loads
    (each chart still has its own 3h/24h/7d toggle)</label>
    <select id="gRange" style="max-width:160px">
      <option value="3">3 hours</option>
      <option value="24">24 hours</option>
      <option value="168">7 days</option>
    </select>
    <label style="display:flex;align-items:center;gap:8px;margin-top:14px">
      <input type="checkbox" id="gUpdateCheck" checked>
      Check GitHub once a day for new versions (the only non-monitoring network call this tool makes)
    </label>
  </div>
  <div class="card">
    <h2 style="margin-top:0">"What's normal" thresholds</h2>
    <div class="sub" style="margin-bottom:10px">Used for the GOOD / FAIR / HIGH badges and chart
    reference lines. Leave a box empty for the built-in default (shown greyed).</div>
    <div class="thresh-grid" id="gThresh">
      <span class="hdr">Metric</span><span class="hdr">Good (up to)</span><span class="hdr">Fair (up to)</span>
    </div>
  </div>
  <div class="card">
    <h2 style="margin-top:0">Event triggers</h2>
    <div class="sub" style="margin-bottom:10px">When the monitor declares real <b>events</b>
    (outages, degraded spells) — the thresholds above only color the badges, these decide what
    lands in the outage log and fires alerts. Leave empty for the defaults (shown greyed).</div>
    <div class="row" style="flex-wrap:wrap; gap:14px">
      <label style="margin:0">Outage after <input type="number" id="dtFails" placeholder="3" min="2" max="10" style="width:70px; margin:0 4px"> failed checks in a row</label>
      <label style="margin:0">Degraded when latency &gt; <input type="number" id="dtLat" placeholder="150" min="50" max="1000" style="width:80px; margin:0 4px"> ms</label>
      <label style="margin:0">or packet loss &gt; <input type="number" id="dtLoss" placeholder="20" min="5" max="80" style="width:70px; margin:0 4px"> %</label>
    </div>
  </div>
  <div class="card">
    <h2 style="margin-top:0">Extra ping targets</h2>
    <div class="sub" style="margin-bottom:4px">Destinations <b>you</b> care about — a game
    server, your work VPN, a relative's router. Up to 5.</div>
    <details class="hint"><summary>More…</summary>
      <div class="sub">The built-in checks use big anycast services
      (the easiest hosts on the internet to reach), so "internet fine but my game is unplayable"
      is invisible without asking the actual destination. Each target gets its own chart line and
      its own outage events ("unreachable while the internet is fine").</div>
    </details>
    <table id="tgTable"><tr><th>Name</th><th>Host or IP</th><th></th></tr></table>
    <div class="row" style="margin-top:10px">
      <button class="small" id="tgAdd">+ Add target</button>
    </div>
  </div>
  <div class="card">
    <h2 style="margin-top:0">Check frequency</h2>
    <div class="sub" style="margin-bottom:4px">How often the monitor runs each check. Saving
    applies within one cycle — no restart.</div>
    <details class="hint"><summary>More…</summary>
      <div class="sub">The dashboard's card footers show the <b>measured</b>
      cadence (marked ~), which can run slower than the setting while checks wait on timeouts —
      e.g. routers that only answer via ARP add a few seconds of every cycle.
      Speed tests download/upload real data: more often than every 15 minutes can eat into data
      caps and briefly loads the line each run.</div>
    </details>
    <div class="iv-grid" id="gIntervals"></div>
  </div>
  <div class="msg" id="gMsg"></div>
  <div class="row" style="justify-content:flex-end"><button class="primary" id="gSave">Save general settings</button></div>
</div>

<div id="tab-routers" style="display:none" role="tabpanel" aria-labelledby="tabbtn-routers" tabindex="0">
  <div class="card">
    <h3 style="margin:0 0 4px">Internet box (ISP modem / ONT)</h3>
    <div class="sub" style="margin-bottom:4px">The box your internet line plugs into,
      <b>before</b> your own router — often at 192.168.100.1 or 192.168.0.1.
      Leave the IP empty if you don't have one.</div>
    <details class="hint"><summary>More…</summary>
      <div class="sub">Monitoring it
      splits "my router died" from "the ISP's box died", and it's drawn on the house wall
      where the line enters, not on a floor. If the box is in bridge mode it usually has
      no LAN address — leave the IP empty then too.</div>
    </details>
    <div class="row" style="margin-bottom:16px">
      <input type="text" id="ispName" placeholder="Name (e.g. ISP Box)" style="max-width:190px">
      <input type="text" id="ispIp" class="mono" placeholder="IP (e.g. 192.168.100.1)" style="max-width:200px">
    </div>
    <h3 style="margin:0 0 6px">Routers &amp; access points</h3>
    <table id="rTable"><tr><th>Name</th><th>IP</th><th>Floor</th><th></th></tr></table>
    <div class="row" style="margin-top:10px">
      <button class="small" id="rAdd">+ Add router</button>
      <span class="grow"></span>
      <button class="small" id="rScan">Scan network for routers</button>
    </div>
    <div id="rScanResults"></div>
  </div>
  <div class="msg" id="rMsg"></div>
  <div class="row" style="justify-content:flex-end"><button class="primary" id="rSave">Save routers</button></div>
</div>

<div id="tab-devices" style="display:none" role="tabpanel" aria-labelledby="tabbtn-devices" tabindex="0">
  <div class="card">
    <div class="sub" style="margin-bottom:4px">Every device the scanner has seen in the last
    30 days, ready to name — no more copying MAC addresses from the dashboard.</div>
    <details class="hint" style="margin-bottom:10px"><summary>More…</summary>
      <div class="sub">Names show on the dashboard's device table and in new-device alerts.
      Give a device a type (camera, printer, ...) to group it in the dashboard's IoT section;
      tick Watch to actively check it every ~30s and log outages when it stops answering.
      Clearing a row's fields (&#10005;) just forgets the name — the device stays listed as
      long as the scanner keeps seeing it.</div>
    </details>
    <div class="row" style="margin-bottom:10px">
      <input type="search" id="dSearch" placeholder="Filter — name, IP, MAC, hostname" autocomplete="off" spellcheck="false" style="max-width:280px">
      <button class="small" id="dScan">Scan for devices now</button>
      <span class="mono" id="dScanCmd" style="font-size:11px"></span>
    </div>
    <table id="dTable"><tr><th>Device</th><th>Type</th><th>Watch</th><th></th></tr></table>
    <div class="row" style="margin-top:10px"><button class="small" id="dAdd">+ Add a device by MAC</button></div>
  </div>
  <div class="msg" id="dMsg"></div>
  <div class="row" style="justify-content:flex-end"><button class="primary" id="dSave">Save device names</button></div>
</div>

<div id="tab-alerts" style="display:none" role="tabpanel" aria-labelledby="tabbtn-alerts" tabindex="0">
  <div class="card">
    <label style="display:flex;align-items:center;gap:8px">
      <input type="checkbox" id="aEnabled"> Enable alerts
    </label>
    <div class="sub" style="margin:6px 0 12px">Notifications fire when a problem has lasted at least
    the minimum duration below, plus a recovery message with the total downtime when it ends.
    While the internet itself is down, webhook/email alerts queue up and send on recovery.</div>
    <label>Alert on</label>
    <div class="row" style="gap:16px;flex-wrap:wrap">
      <label style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="aEvOutage" checked> Outages</label>
      <label style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="aEvDegraded"> Slow / degraded</label>
      <label style="display:flex;align-items:center;gap:6px" title="Repeated micro-drops (each too short to be an outage) within an hour"><input type="checkbox" id="aEvInstability"> Flapping</label>
      <label style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="aEvNewDevice" checked> New devices</label>
      <label style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="aEvIpChange"> Public IP changes</label>
      <label style="display:flex;align-items:center;gap:6px" title="Watched devices from the Devices tab (cameras, printers, ...) going unreachable"><input type="checkbox" id="aEvIot"> IoT devices (watched)</label>
    </div>
    <div class="row" style="margin-top:10px">
      <div><label>Min duration before alerting (seconds)</label>
        <input type="number" id="aMinDur" min="0" max="3600" placeholder="60" style="width:130px"></div>
      <div><label>Repeat cooldown (minutes)</label>
        <input type="number" id="aCooldown" min="0" max="1440" placeholder="5" style="width:130px"></div>
    </div>
    <label>Quiet hours (no desktop popups; webhook/email wait until the window ends — leave empty for none)</label>
    <div class="row">
      <input type="time" id="aQuietStart" style="width:120px">
      <span>to</span>
      <input type="time" id="aQuietEnd" style="width:120px">
    </div>
  </div>
  <div class="card">
    <h2 style="margin-top:0">Desktop popup (this PC)</h2>
    <label style="display:flex;align-items:center;gap:8px">
      <input type="checkbox" id="aToast" checked> Show a desktop notification on the monitor PC
    </label>
  </div>
  <div class="card">
    <h2 style="margin-top:0">Webhook</h2>
    <div class="sub" style="margin-bottom:4px">Simplest phone push: make a topic at
    <span class="mono">ntfy.sh</span>, put <span class="mono">https://ntfy.sh/your-topic</span> here
    with format <span class="mono">ntfy</span>, and install their app.</div>
    <details class="hint"><summary>More…</summary>
      <div class="sub">"json" posts {title, message, …} for Slack/Discord-style receivers;
      "text" posts the plain message body.</div>
    </details>
    <label style="display:flex;align-items:center;gap:8px">
      <input type="checkbox" id="aWebhook"> Enabled
    </label>
    <label>URL</label>
    <input type="text" id="aWebhookUrl" placeholder="https://ntfy.sh/my-home-network">
    <label>Format</label>
    <select id="aWebhookFmt" style="width:130px">
      <option value="json">json</option><option value="ntfy">ntfy</option><option value="text">text</option>
    </select>
  </div>
  <div class="card">
    <h2 style="margin-top:0">Email</h2>
    <div class="sub" style="margin-bottom:8px">The password is stored in plain text in
    <span class="mono">config.json</span> on this PC (never leaves it, never shared over the LAN) —
    use a dedicated <b>app password</b>, not your real account password.</div>
    <label style="display:flex;align-items:center;gap:8px">
      <input type="checkbox" id="aEmail"> Enabled
    </label>
    <div class="row">
      <div><label>SMTP host</label><input type="text" id="aEmailHost" placeholder="smtp.gmail.com" style="width:200px"></div>
      <div><label>Port</label><input type="number" id="aEmailPort" placeholder="587" min="1" max="65535" style="width:90px"></div>
      <div><label style="display:flex;align-items:center;gap:6px;margin-top:24px"><input type="checkbox" id="aEmailTls" checked> STARTTLS</label></div>
    </div>
    <div class="row">
      <div><label>Username</label><input type="text" id="aEmailUser" style="width:200px"></div>
      <div><label>App password</label><input type="password" id="aEmailPass" style="width:200px"></div>
    </div>
    <div class="row">
      <div><label>From</label><input type="text" id="aEmailFrom" placeholder="netmon@home" style="width:200px"></div>
    </div>
    <label>Send to</label>
    <div id="aEmailToList"></div>
    <div class="row" style="margin-top:6px"><button class="small" id="aEmailToAdd">+ Add recipient</button></div>
  </div>
  <div class="msg" id="aMsg"></div>
  <div class="row" style="justify-content:flex-end">
    <button class="small" id="aTest">Send test alert</button>
    <span class="grow"></span>
    <button class="primary" id="aSave">Save alert settings</button>
  </div>
</div>

<div class="footer">Changes apply on their own: routers within ~15 s, device names within
~5 min, general settings on the next dashboard refresh (~1 min). No restarts needed.</div>

<script>
""" + _SHARED_JS + """
// Built-in dashboard defaults, shown as placeholders (source of truth for
// actual rating lives in dashboard.py's JS THRESHOLDS).
const THRESH_METRICS = [
  ['latency', 'Latency ms', 40, 100],
  ['jitter', 'Jitter ms', 10, 30],
  ['dns', 'DNS ms', 40, 100],
  ['loss', 'Loss %', 1, 2.5],
  ['plan_pct', 'Speed, % of plan', 90, 80],
  ['uptime', 'Uptime %', 99.9, 99],
];
// [key, label, default seconds, preset choices] — defaults and the
// allowed range mirror monitor.py's INTERVAL_DEFAULTS/INTERVAL_BOUNDS
// (validated server-side in settings_api.validate_config).
const INTERVAL_CHECKS = [
  ['ping',      'Internet ping (uptime, latency, jitter)', 15,   [5, 10, 15, 30, 60, 120, 300]],
  ['router',    'Router / access point checks',            15,   [10, 15, 30, 60, 120, 300]],
  ['dns',       'DNS lookup',                              60,   [15, 30, 60, 120, 300, 900]],
  ['wifi',      'Wi-Fi signal snapshot',                   300,  [60, 120, 300, 600, 1800, 3600]],
  ['devices',   'Device scan',                             300,  [60, 120, 300, 600, 1800, 3600]],
  ['iot',       'IoT device watch',                        30,   [10, 15, 30, 60, 120, 300, 600]],
  ['speedtest', 'Speed test',                              1800, [900, 1800, 3600, 10800, 21600, 43200, 86400]],
  ['public_ip', 'Public IP check',                         600,  [120, 300, 600, 1800, 3600]],
];
function fmtIv(s) { return s < 60 ? s + 's' : s < 3600 ? (s / 60) + ' min' : (s / 3600) + ' h'; }
const S = { config: {}, routers: [], devices: {}, census: {} };
S.floorState = { floors: [], groundIndex: 0, main: null, onchange: refreshRouterFloorSelects };

// ---- tabs (ARIA tablist: click + arrow-key navigation, roving tabindex) ----
function activateTab(b) {
  for (const x of document.querySelectorAll('.tabs button')) {
    const on = x === b;
    x.className = on ? 'active' : '';
    x.setAttribute('aria-selected', on ? 'true' : 'false');
    x.tabIndex = on ? 0 : -1;
  }
  for (const name of ['general','routers','devices','alerts']) {
    document.getElementById('tab-' + name).style.display = name === b.dataset.tab ? '' : 'none';
  }
  // remember the tab in the URL so a refresh doesn't bounce to General
  try { history.replaceState(null, '', '#' + b.dataset.tab); } catch (e) {}
}
document.querySelector('.tabs').onclick = (e) => {
  const b = e.target.closest('button'); if (!b) return;
  activateTab(b);
};
document.querySelector('.tabs').onkeydown = (e) => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
  const btns = Array.from(document.querySelectorAll('.tabs button'));
  const cur = btns.indexOf(document.activeElement);
  if (cur === -1) return;
  let next = cur;
  if (e.key === 'ArrowLeft') next = (cur + btns.length - 1) % btns.length;
  else if (e.key === 'ArrowRight') next = (cur + 1) % btns.length;
  else if (e.key === 'Home') next = 0;
  else next = btns.length - 1;
  e.preventDefault();
  btns[next].focus();
  activateTab(btns[next]);
};

// ---- unsaved-changes guard ----
// Saves are whole-file last-writer-wins, so silently navigating away has
// really lost edits before. Each tab tracks its own dirty flag: an amber
// dot on the tab button + a leave-page warning while any flag is set.
const DIRTY = {};
function markDirty(tab) { if (!DIRTY[tab]) { DIRTY[tab] = true; syncDirtyDots(); } }
function clearDirty(tab) { DIRTY[tab] = false; syncDirtyDots(); }
function syncDirtyDots() {
  for (const name of ['general','routers','devices','alerts']) {
    const btn = document.getElementById('tabbtn-' + name);
    if (btn) btn.classList.toggle('dirty', !!DIRTY[name]);
  }
}
for (const name of ['general','routers','devices','alerts']) {
  const panel = document.getElementById('tab-' + name);
  if (!panel) continue;
  panel.addEventListener('input', (e) => {
    if (e.target && e.target.id === 'dSearch') return;   // filtering edits nothing
    markDirty(name);
  });
  panel.addEventListener('change', (e) => {
    if (e.target && e.target.id === 'dSearch') return;
    markDirty(name);
  });
  // row add/delete/reorder are button clicks, not input events. Saves,
  // the test-alert button, and the read-only network scan stay clean.
  panel.addEventListener('click', (e) => {
    const b = e.target.closest ? e.target.closest('button') : null;
    if (b && !b.classList.contains('primary') && b.id !== 'aTest' && b.id !== 'rScan') markDirty(name);
  });
}
window.addEventListener('beforeunload', (e) => {
  if (Object.keys(DIRTY).some(k => DIRTY[k])) { e.preventDefault(); e.returnValue = ''; }
});

// ---- load ----
async function load() {
  const {status, data} = await api('/api/config');
  if (status !== 200) {
    showMsg(document.getElementById('gMsg'), 'error', 'Could not load settings (HTTP ' + status + ').');
    return;
  }
  S.config = data.config || {};
  S.routers = data.routers || [];
  S.devices = data.devices || {};
  S.census = data.census || {};
  document.getElementById('dScanCmd').textContent =
    'runs: ' + ((data.meta && data.meta.scan_cmd) || 'ping sweep + arp');

  document.getElementById('gTitle').value = S.config.title || 'Home Network Monitor';
  S.floorState.floors = (S.config.floors || []).slice();
  S.floorState.groundIndex = groundIndexFrom(S.floorState.floors, S.config.underground_floors);
  S.floorState.main = S.config.main_router_floor || null;
  renderFloors(S.floorState, 'gFloors');
  document.getElementById('gPlanDown').value = S.config.plan_down_mbps || '';
  document.getElementById('gPlanUp').value = S.config.plan_up_mbps || '';
  document.getElementById('gUpdateCheck').checked = S.config.update_check !== false;

  // monitor location: Main Router + every router INCLUDING the ISP box —
  // a PC plugged straight into the ISP box measures the raw line, which
  // is the best possible evidence vantage
  const locSel = document.getElementById('gMonLoc');
  const locCur = S.config.monitor_location || '';
  const locNames = ['Main Router'].concat(S.routers.map(r => r.name).filter(Boolean));
  if (locCur && !locNames.includes(locCur)) locNames.push(locCur);
  locSel.innerHTML = '<option value="">(not set)</option>' + locNames.map(n =>
    '<option' + (n === locCur ? ' selected' : '') + '>' + esc(n) + '</option>').join('');

  document.getElementById('gRange').value = String(S.config.default_range_hours || 3);

  const dt = S.config.detection || {};
  document.getElementById('dtFails').value = dt.outage_fails != null ? dt.outage_fails : '';
  document.getElementById('dtLat').value = dt.degraded_latency_ms != null ? dt.degraded_latency_ms : '';
  document.getElementById('dtLoss').value = dt.degraded_loss_pct != null ? dt.degraded_loss_pct : '';

  const tgT = document.getElementById('tgTable');
  tgT.innerHTML = '<tr><th>Name</th><th>Host or IP</th><th></th></tr>' +
    (S.config.custom_targets || []).map(targetRow).join('');

  const grid = document.getElementById('gThresh');
  const th = S.config.thresholds || {};
  for (const [key, lbl, defGood, defFair] of THRESH_METRICS) {
    grid.insertAdjacentHTML('beforeend',
      '<span>' + lbl + '</span>' +
      '<input type="number" step="any" id="th-' + key + '-good" placeholder="' + defGood + '" value="' + (th[key] && th[key].good != null ? th[key].good : '') + '">' +
      '<input type="number" step="any" id="th-' + key + '-fair" placeholder="' + defFair + '" value="' + (th[key] && th[key].fair != null ? th[key].fair : '') + '">');
  }

  const ivGrid = document.getElementById('gIntervals');
  const iv = S.config.intervals || {};
  for (const [key, lbl, def, choices] of INTERVAL_CHECKS) {
    const cur = iv[key];
    let opts = '<option value="">default (' + fmtIv(def) + ')</option>';
    // a hand-edited config.json value outside the presets still shows up
    const vals = choices.slice();
    if (cur != null && !vals.includes(cur)) vals.push(cur), vals.sort((a, b) => a - b);
    for (const v of vals) {
      opts += '<option value="' + v + '"' + (cur === v ? ' selected' : '') + '>' + fmtIv(v) + '</option>';
    }
    ivGrid.insertAdjacentHTML('beforeend',
      '<span>' + lbl + '</span><select id="iv-' + key + '">' + opts + '</select>');
  }

  renderRouters();
  renderDevices();
  loadAlerts();
}

// ---- alerts tab ----
function loadAlerts() {
  const a = S.config.alerts || {};
  const ev = a.events || {};
  const ch = a.channels || {};
  document.getElementById('aEnabled').checked = !!a.enabled;
  document.getElementById('aEvOutage').checked = ev.outage !== false;
  document.getElementById('aEvDegraded').checked = !!ev.degraded;
  document.getElementById('aEvInstability').checked = !!ev.instability;
  document.getElementById('aEvNewDevice').checked = ev.new_device !== false;
  document.getElementById('aEvIpChange').checked = !!ev.ip_change;
  document.getElementById('aEvIot').checked = !!ev.iot_outage;
  document.getElementById('aMinDur').value = a.min_duration_sec != null ? a.min_duration_sec : '';
  document.getElementById('aCooldown').value = a.cooldown_minutes != null ? a.cooldown_minutes : '';
  // hand-edited configs may hold "7:00" — pad to "07:00" or the time
  // input rejects it (shows empty) and the next save silently drops it
  function padTime(v) {
    if (!v || v.indexOf(':') === -1) return v || '';
    const parts = v.split(':');
    return parts[0].padStart(2, '0') + ':' + parts[1].padStart(2, '0');
  }
  document.getElementById('aQuietStart').value = padTime((a.quiet_hours || {}).start);
  document.getElementById('aQuietEnd').value = padTime((a.quiet_hours || {}).end);
  document.getElementById('aToast').checked = !ch.toast || ch.toast.enabled !== false;
  const wh = ch.webhook || {};
  document.getElementById('aWebhook').checked = !!wh.enabled;
  document.getElementById('aWebhookUrl').value = wh.url || '';
  document.getElementById('aWebhookFmt').value = wh.format || 'json';
  const em = ch.email || {};
  document.getElementById('aEmail').checked = !!em.enabled;
  document.getElementById('aEmailHost').value = em.host || '';
  document.getElementById('aEmailPort').value = em.port != null ? em.port : '';
  document.getElementById('aEmailTls').checked = em.starttls !== false;
  document.getElementById('aEmailUser').value = em.username || '';
  document.getElementById('aEmailPass').value = em.password || '';
  document.getElementById('aEmailFrom').value = em.from || '';
  // one input row per recipient (hand-edited configs may hold a JSON
  // array OR a comma/semicolon string — both render as rows; the save
  // writes the array form back, which the monitor accepts equally)
  const tos = Array.isArray(em.to) ? em.to
    : String(em.to || '').split(/[,;]/).map(s => s.trim()).filter(Boolean);
  renderEmailTo(tos.length ? tos : ['']);
}

function collectAlerts() {
  const a = { enabled: document.getElementById('aEnabled').checked };
  a.events = {
    outage: document.getElementById('aEvOutage').checked,
    degraded: document.getElementById('aEvDegraded').checked,
    instability: document.getElementById('aEvInstability').checked,
    new_device: document.getElementById('aEvNewDevice').checked,
    ip_change: document.getElementById('aEvIpChange').checked,
    iot_outage: document.getElementById('aEvIot').checked,
  };
  const md = parseFloat(document.getElementById('aMinDur').value);
  if (!isNaN(md)) a.min_duration_sec = md;
  const cd = parseFloat(document.getElementById('aCooldown').value);
  if (!isNaN(cd)) a.cooldown_minutes = cd;
  const qs = document.getElementById('aQuietStart').value.trim();
  const qe = document.getElementById('aQuietEnd').value.trim();
  if (qs && qe) a.quiet_hours = { start: qs, end: qe };
  a.channels = {
    toast: { enabled: document.getElementById('aToast').checked },
    webhook: {
      enabled: document.getElementById('aWebhook').checked,
      url: document.getElementById('aWebhookUrl').value.trim(),
      format: document.getElementById('aWebhookFmt').value,
    },
    email: {
      enabled: document.getElementById('aEmail').checked,
      host: document.getElementById('aEmailHost').value.trim(),
      port: parseInt(document.getElementById('aEmailPort').value, 10) || 587,
      starttls: document.getElementById('aEmailTls').checked,
      username: document.getElementById('aEmailUser').value.trim(),
      password: document.getElementById('aEmailPass').value,
      from: document.getElementById('aEmailFrom').value.trim(),
      to: emailToValues(),
    },
  };
  return a;
}

// ---- email recipient rows ----
function emailToRow(addr) {
  return '<div class="row" style="margin-bottom:6px;align-items:center">' +
    '<input type="email" class="a-email-to" placeholder="you@example.com" value="' + esc(addr) + '" style="width:260px">' +
    '<button class="small danger a-email-to-del" title="Remove this recipient">&#10005;</button></div>';
}
function renderEmailTo(list) {
  document.getElementById('aEmailToList').innerHTML = list.map(emailToRow).join('');
}
function emailToValues() {
  return [...document.querySelectorAll('.a-email-to')].map(i => i.value.trim()).filter(Boolean);
}
document.getElementById('aEmailToList').onclick = (e) => {
  if (!e.target.classList.contains('a-email-to-del')) return;
  e.target.closest('.row').remove();
  // never leave zero inputs — an empty row is the "type here" affordance
  if (!document.querySelectorAll('.a-email-to').length) renderEmailTo(['']);
};
document.getElementById('aEmailToAdd').onclick = () => {
  document.getElementById('aEmailToList').insertAdjacentHTML('beforeend', emailToRow(''));
  const rows = document.querySelectorAll('.a-email-to');
  rows[rows.length - 1].focus();
};

document.getElementById('aSave').onclick = async () => {
  const msg = document.getElementById('aMsg');
  clearMsg(msg);
  const cfg = Object.assign({}, S.config);   // keep unknown keys as-is
  cfg.alerts = collectAlerts();
  const {status, data} = await api('/api/config', {config: cfg});
  if (status === 200) {
    S.config = cfg;
    clearDirty('alerts');
    showMsg(msg, 'ok', 'Saved. The monitor picks this up within ~10 seconds — no restart.',
      (data.warnings || []).map(fmtIssue));
  } else {
    showMsg(msg, 'error', 'Not saved:', (data.errors || []).map(fmtIssue));
  }
};

document.getElementById('aTest').onclick = async () => {
  const msg = document.getElementById('aMsg');
  clearMsg(msg);
  const {status} = await api('/api/alerts/test', {});
  showMsg(msg, status === 202 ? 'ok' : 'error', status === 202
    ? 'Test alert queued — it goes to every enabled channel within a few seconds. Save first if you just changed settings.'
    : 'Could not queue the test alert (HTTP ' + status + ').');
};

// ---- general save ----
document.getElementById('gAddFloor').onclick = () => addFloor(S.floorState, 'gFloors');
document.getElementById('gAddBasement').onclick = () => addBasement(S.floorState, 'gFloors');
document.getElementById('gSave').onclick = async () => {
  const msg = document.getElementById('gMsg');
  clearMsg(msg);
  const cfg = Object.assign({}, S.config);  // keep unknown keys as-is
  cfg.title = document.getElementById('gTitle').value.trim() || 'Home Network Monitor';
  const all = S.floorState.floors.map(f => f.trim());
  const fs = all.filter(Boolean);
  if (fs.length) {
    cfg.floors = fs;
    cfg.underground_floors = all.slice(S.floorState.groundIndex).filter(Boolean);
    cfg.main_router_floor = fs.includes(S.floorState.main) ? S.floorState.main : fs[fs.length - 1];
  } else { delete cfg.floors; delete cfg.underground_floors; delete cfg.main_router_floor; }
  // NB hide_ip_prefixes intentionally has NO UI field (it confused more
  // than it clarified) — it still works when hand-set in config.json,
  // and Object.assign above carries an existing value through untouched.
  const pd = parseFloat(document.getElementById('gPlanDown').value);
  const pu = parseFloat(document.getElementById('gPlanUp').value);
  if (pd > 0) cfg.plan_down_mbps = pd; else delete cfg.plan_down_mbps;
  if (pu > 0) cfg.plan_up_mbps = pu; else delete cfg.plan_up_mbps;
  if (document.getElementById('gUpdateCheck').checked) delete cfg.update_check;
  else cfg.update_check = false;
  const monLoc = document.getElementById('gMonLoc').value;
  if (monLoc) cfg.monitor_location = monLoc; else delete cfg.monitor_location;
  const dRange = parseInt(document.getElementById('gRange').value, 10);
  if (dRange === 3) delete cfg.default_range_hours;   // 3h IS the built-in default
  else cfg.default_range_hours = dRange;
  const dt = {};
  const dtFails = parseInt(document.getElementById('dtFails').value, 10);
  const dtLat = parseFloat(document.getElementById('dtLat').value);
  const dtLoss = parseFloat(document.getElementById('dtLoss').value);
  if (!isNaN(dtFails)) dt.outage_fails = dtFails;
  if (!isNaN(dtLat)) dt.degraded_latency_ms = dtLat;
  if (!isNaN(dtLoss)) dt.degraded_loss_pct = dtLoss;
  if (Object.keys(dt).length) cfg.detection = dt; else delete cfg.detection;
  const tgs = [];
  for (const tr of document.querySelectorAll('#tgTable tr')) {
    const n = tr.querySelector('.tg-name');
    if (!n) continue;
    const name = n.value.trim(), host = tr.querySelector('.tg-host').value.trim();
    if (name || host) tgs.push({name: name, host: host});
  }
  if (tgs.length) cfg.custom_targets = tgs; else delete cfg.custom_targets;
  // carry through threshold keys that no longer have a UI row (e.g. a
  // legacy wifi entry) — rebuilding from the rows alone would drop them
  const th = {};
  const existingTh = S.config.thresholds || {};
  for (const k of Object.keys(existingTh)) {
    if (!THRESH_METRICS.some(m => m[0] === k)) th[k] = existingTh[k];
  }
  for (const [key] of THRESH_METRICS) {
    const good = parseFloat(document.getElementById('th-' + key + '-good').value);
    const fair = parseFloat(document.getElementById('th-' + key + '-fair').value);
    const entry = {};
    if (!isNaN(good)) entry.good = good;
    if (!isNaN(fair)) entry.fair = fair;
    if (Object.keys(entry).length) th[key] = entry;
  }
  if (Object.keys(th).length) cfg.thresholds = th; else delete cfg.thresholds;
  const iv = {};
  for (const [key] of INTERVAL_CHECKS) {
    const v = parseInt(document.getElementById('iv-' + key).value, 10);
    if (!isNaN(v)) iv[key] = v;   // empty = use the default, stored as absent
  }
  if (Object.keys(iv).length) cfg.intervals = iv; else delete cfg.intervals;

  const {status, data} = await api('/api/config', {config: cfg});
  if (status === 200) {
    S.config = cfg;
    clearDirty('general');
    showMsg(msg, 'ok', 'Saved. The dashboard picks this up on its next refresh (~1 min); '
      + 'check frequencies apply within one old cycle of each check.',
      (data.warnings || []).map(fmtIssue));
  } else {
    showMsg(msg, 'error', 'Not saved — please fix:', (data && data.errors || []).map(fmtIssue));
  }
};

// ---- custom ping targets (General tab) ----
function targetRow(t) {
  return '<tr>' +
    '<td><input type="text" class="tg-name" maxlength="40" value="' + esc(t.name || '') + '" placeholder="e.g. Game server"></td>' +
    '<td><input type="text" class="tg-host mono" value="' + esc(t.host || '') + '" placeholder="host or IP"></td>' +
    '<td><button class="small danger tg-del">&#10005;</button></td></tr>';
}
document.getElementById('tgTable').onclick = (e) => {
  if (e.target.classList.contains('tg-del')) e.target.closest('tr').remove();
};
document.getElementById('tgAdd').onclick = () => {
  if (document.querySelectorAll('#tgTable .tg-name').length >= 5) return;
  document.getElementById('tgTable').insertAdjacentHTML('beforeend', targetRow({name: '', host: ''}));
};

// ---- routers ----
function routerRow(r) {
  return '<tr>' +
    '<td><input type="text" class="r-name" value="' + esc(r.name || '') + '"></td>' +
    '<td><input type="text" class="r-ip mono" value="' + esc(r.ip || '') + '"></td>' +
    '<td><select class="r-floor">' + settingsFloorOptions(r.floor) + '</select></td>' +
    '<td><button class="small danger r-del">&#10005;</button></td></tr>';
}
function settingsFloorOptions(sel) {
  const fs = S.floorState.floors.length ? S.floorState.floors : [sel].filter(Boolean);
  let opts = '<option value="">(none)</option>';
  for (const f of fs) opts += '<option' + (f === sel ? ' selected' : '') + '>' + esc(f) + '</option>';
  if (sel && !fs.includes(sel)) opts += '<option selected>' + esc(sel) + '</option>';
  return opts;
}
function renderRouters() {
  // the role:'isp' entry lives in its own two fields above the table —
  // it's monitored like any router but it isn't an AP on a floor
  const isp = S.routers.find(r => r.role === 'isp');
  document.getElementById('ispName').value = isp ? (isp.name || '') : '';
  document.getElementById('ispIp').value = isp ? (isp.ip || '') : '';
  const t = document.getElementById('rTable');
  t.innerHTML = '<tr><th>Name</th><th>IP</th><th>Floor</th><th></th></tr>' +
    S.routers.filter(r => r.role !== 'isp').map(routerRow).join('');
}
function refreshRouterFloorSelects() {
  for (const sel of document.querySelectorAll('.r-floor')) {
    const cur = sel.value;
    sel.innerHTML = settingsFloorOptions(cur);
  }
}
function collectRouters() {
  const out = [];
  const ispIp = document.getElementById('ispIp').value.trim();
  if (ispIp) {
    out.push({name: document.getElementById('ispName').value.trim() || 'ISP Box',
              ip: ispIp, role: 'isp'});
  }
  for (const tr of document.querySelectorAll('#rTable tr')) {
    const name = tr.querySelector('.r-name');
    if (!name) continue;
    const entry = {name: name.value.trim(), ip: tr.querySelector('.r-ip').value.trim()};
    const floor = tr.querySelector('.r-floor').value;
    if (floor) entry.floor = floor;
    if (entry.name || entry.ip) out.push(entry);
  }
  return out;
}
document.getElementById('rTable').onclick = (e) => {
  if (e.target.classList.contains('r-del')) e.target.closest('tr').remove();
};
document.getElementById('rAdd').onclick = () => {
  document.getElementById('rTable').insertAdjacentHTML('beforeend',
    routerRow({name: '', ip: '', floor: S.floorState.main || ''}));
};
document.getElementById('rSave').onclick = async () => {
  const msg = document.getElementById('rMsg');
  clearMsg(msg);
  const routers = collectRouters();
  const {status, data} = await api('/api/config', {routers});
  if (status === 200) {
    S.routers = routers;
    clearDirty('routers');
    showMsg(msg, 'ok', 'Saved. The monitor starts watching the new list within ~15 seconds.',
      (data.warnings || []).map(fmtIssue));
  } else {
    showMsg(msg, 'error', 'Not saved — please fix:', (data && data.errors || []).map(fmtIssue));
  }
};

// scan-for-routers inside settings
document.getElementById('rScan').onclick = async () => {
  const box = document.getElementById('rScanResults');
  box.innerHTML = '<div class="mono" style="margin-top:10px">Scanning… takes 30–60 s.</div>';
  await api('/api/discover', {});
  const tick = async () => {
    const {data} = await api('/api/discover/status');
    if (!data) { setTimeout(tick, 2000); return; }
    if (data.state === 'running') {
      const pct = data.total ? Math.round(100 * data.done / data.total) : 0;
      box.innerHTML = '<div class="mono" style="margin-top:10px">Scanning… ' + pct + '%</div>';
      setTimeout(tick, 2000);
    } else if (data.state === 'done') {
      const rows = (data.results || []).filter(r => !r.ping_only || r.known_router_name);
      if (!rows.length) { box.innerHTML = '<div class="mono" style="margin-top:10px">Nothing with an admin page found.</div>'; return; }
      box.innerHTML = '<table style="margin-top:10px"><tr><th>IP</th><th>Looks like</th><th></th></tr>' +
        rows.map((r, i) =>
          '<tr><td class="mono">' + esc(r.ip) + '</td>' +
          '<td class="mono">' + esc(r.title || r.server || r.hostname || '?') +
            (r.is_gateway ? ' <span class="badge gateway">main gateway</span>' : '') +
            (r.known_router_name ? ' <span class="badge known">monitored as "' + esc(r.known_router_name) + '"</span>' : '') + '</td>' +
          '<td>' + (r.known_router_name || r.is_gateway ? '' :
            '<button class="small scan-add" data-ip="' + esc(r.ip) + '" data-nm="' + esc(r.title || r.hostname || ('Router at ' + r.ip)) + '">Add</button>') + '</td></tr>'
        ).join('') + '</table>';
      box.onclick = (e) => {
        const b = e.target.closest('.scan-add'); if (!b) return;
        document.getElementById('rTable').insertAdjacentHTML('beforeend',
          routerRow({name: b.dataset.nm, ip: b.dataset.ip, floor: S.floorState.main || ''}));
        b.disabled = true; b.textContent = 'Added';
      };
    } else if (data.state === 'error') {
      box.innerHTML = '<div class="mono" style="margin-top:10px">Scan failed: ' + esc(data.error || '?') + '</div>';
    }
  };
  setTimeout(tick, 2000);
};

// ---- devices ----
// devices.json values: a plain name string, or {name, type, watch} for IoT
// devices — mirrors monitor.py / settings_api.py IOT_TYPES.
const IOT_TYPES = ['camera', 'intercom', 'printer', 'light', 'plug', 'speaker', 'tv', 'other'];
function timeAgo(ts) {
  if (!ts) return '';
  const secs = (Date.now() - new Date(ts).getTime()) / 1000;
  if (isNaN(secs)) return '';
  if (secs < 3600) return Math.max(1, Math.round(secs / 60)) + 'm ago';
  if (secs < 86400) return Math.round(secs / 3600) + 'h ago';
  return Math.round(secs / 86400) + 'd ago';
}
function typeOpts(sel) {
  return ['<option value="">(none)</option>'].concat(IOT_TYPES.map(t =>
    '<option value="' + t + '"' + (sel === t ? ' selected' : '') + '>' +
    t.charAt(0).toUpperCase() + t.slice(1) + '</option>')).join('');
}
// One row per device the scanner has SEEN (S.census, from the collector's
// DB) plus any devices.json entries it hasn't - so naming a device means
// finding it by its IP/hostname/online state right here, not copying a
// MAC across from the dashboard.
function deviceRow(mac, value, ctx) {
  const v = typeof value === 'string' ? {name: value} : (value || {});
  const bits = [];
  if (ctx) {
    if (ctx.online) bits.push('<span class="on">online</span>');
    else if (ctx.last_seen) bits.push('seen ' + esc(timeAgo(ctx.last_seen)));
    if (ctx.ip) bits.push(esc(ctx.ip));
    if (ctx.hostname) bits.push(esc(ctx.hostname));
  } else {
    bits.push('not seen in 30 days');
  }
  // the ✕ only shows when there is something to forget — on a bare census
  // row it was a silent no-op that read as "the button is broken"
  const hasEntry = !!(v.name || v.type || v.watch);
  return '<tr class="d-row" data-mac="' + esc(mac) + '">' +
    '<td><input type="text" class="d-name" value="' + esc(v.name || '') +
      '" placeholder="' + esc(ctx && ctx.hostname ? ctx.hostname : 'name this device') + '">' +
    '<div class="d-ctx">' + esc(mac) + ' · ' + bits.join(' · ') +
    '<span class="d-forgot" style="display:none"> — entry forgotten on save</span></div></td>' +
    '<td><select class="d-type">' + typeOpts(v.type) + '</select></td>' +
    '<td style="text-align:center"><input type="checkbox" class="d-watch"' + (v.watch ? ' checked' : '') +
      ' title="Actively check every ~30s and log outages when it stops answering"></td>' +
    '<td><button class="small danger d-del" title="Forget the name/type/watch"' +
      (hasEntry ? '' : ' style="visibility:hidden"') + '>&#10005;</button></td></tr>';
}
// manual escape hatch for a device the scanner has never seen (e.g. a
// printer that's been off for months): editable MAC field
function manualDeviceRow() {
  return '<tr class="d-row" data-manual="1">' +
    '<td><input type="text" class="d-mac mono" placeholder="aa:bb:cc:dd:ee:ff" style="max-width:200px">' +
    '<input type="text" class="d-name" placeholder="name" style="margin-top:4px"></td>' +
    '<td><select class="d-type">' + typeOpts('') + '</select></td>' +
    '<td style="text-align:center"><input type="checkbox" class="d-watch"></td>' +
    '<td><button class="small danger d-del">&#10005;</button></td></tr>';
}
function renderDevices() {
  const seen = Object.keys(S.census);
  const unseen = Object.keys(S.devices).filter(m => !S.census[m]);
  // numeric IP ascending (his call — a stable, guessable order);
  // vanished-but-named entries follow, sorted by MAC
  const ipKey = (ip) => {
    const parts = String(ip || '').split('.').map(n => parseInt(n, 10));
    while (parts.length < 4) parts.push(999);
    return parts.map(n => isNaN(n) ? 999 : n);
  };
  seen.sort((a, b) => {
    const ka = ipKey(S.census[a].ip), kb = ipKey(S.census[b].ip);
    for (let i = 0; i < 4; i++) { if (ka[i] !== kb[i]) return ka[i] - kb[i]; }
    return a.localeCompare(b);
  });
  unseen.sort();
  let html = '<tr><th>Device</th><th>Type</th><th>Watch</th><th></th></tr>';
  html += seen.map(m => deviceRow(m, S.devices[m], S.census[m])).join('');
  if (seen.length && unseen.length) {
    html += '<tr class="d-group"><td colspan="4">Named, but not seen in 30 days</td></tr>';
  }
  html += unseen.map(m => deviceRow(m, S.devices[m], null)).join('');
  document.getElementById('dTable').innerHTML = html;
  filterDevices();
}
function filterDevices() {
  const q = (document.getElementById('dSearch').value || '').trim().toLowerCase();
  for (const tr of document.querySelectorAll('#dTable tr.d-row')) {
    const nm = tr.querySelector('.d-name');
    const hay = ((tr.dataset.mac || '') + ' ' + (nm ? nm.value : '') + ' ' + tr.textContent).toLowerCase();
    tr.style.display = (!q || hay.indexOf(q) !== -1) ? '' : 'none';
  }
  for (const tr of document.querySelectorAll('#dTable tr.d-group')) tr.style.display = q ? 'none' : '';
}
document.getElementById('dSearch').oninput = filterDevices;
document.getElementById('dTable').onclick = (e) => {
  if (!e.target.classList.contains('d-del')) return;
  const tr = e.target.closest('tr');
  if (tr.dataset.manual) { tr.remove(); return; }
  // census-backed rows stay listed - clearing the fields just drops the
  // devices.json entry on the next save. Make that VISIBLE: fade the row,
  // say so on the context line, and hide the now-pointless ✕.
  tr.querySelector('.d-name').value = '';
  tr.querySelector('.d-type').value = '';
  tr.querySelector('.d-watch').checked = false;
  tr.classList.add('d-cleared');
  const fg = tr.querySelector('.d-forgot');
  if (fg) fg.style.display = '';
  e.target.style.visibility = 'hidden';
};
// typing in a row un-fades it and brings the ✕ back (there is something
// to forget again)
function dRowEdited(e) {
  const tr = e.target.closest ? e.target.closest('tr.d-row') : null;
  if (!tr) return;
  tr.classList.remove('d-cleared');
  const fg = tr.querySelector('.d-forgot');
  if (fg) fg.style.display = 'none';
  const del = tr.querySelector('.d-del');
  if (del) del.style.visibility = '';
}
document.getElementById('dTable').addEventListener('input', dRowEdited);
document.getElementById('dTable').addEventListener('change', dRowEdited);
document.getElementById('dAdd').onclick = () => {
  document.getElementById('dTable').insertAdjacentHTML('beforeend', manualDeviceRow());
  const rows = document.querySelectorAll('#dTable tr.d-row');
  const last = rows[rows.length - 1];
  if (last) last.querySelector('.d-mac').focus();
};
// on-demand device sweep: the monitor picks the command up within ~2s,
// sweeps (nmap or ping sweep + arp), and we re-pull the census when the
// status file says it finished
document.getElementById('dScan').onclick = async () => {
  const btn = document.getElementById('dScan');
  const msg = document.getElementById('dMsg');
  clearMsg(msg);
  btn.disabled = true;
  btn.textContent = 'Scanning…';
  const done = (label) => { btn.disabled = false; btn.textContent = 'Scan for devices now'; if (label) showMsg(msg, 'ok', label); };
  const fail = (label) => { btn.disabled = false; btn.textContent = 'Scan for devices now'; showMsg(msg, 'error', label); };
  const {status} = await api('/api/devices/scan', {});
  if (status === 429) { fail('Please wait a minute between scans.'); return; }
  if (status === 409) { fail('A test or scan is already running — try again shortly.'); return; }
  if (status !== 202) { fail('Could not start the scan (HTTP ' + status + ') — is the monitor running?'); return; }
  let waited = 0;
  const timer = setInterval(async () => {
    waited += 2;
    const {status: s, data: st} = await api('/api/test/status');
    if (s === 200 && st && st.state === 'done') {
      clearInterval(timer);
      const n = st.results && st.results.devices_found;
      const {status: cs, data: cfg} = await api('/api/config');
      if (cs === 200) { S.census = cfg.census || {}; S.devices = cfg.devices || {}; renderDevices(); }
      done('Scan finished' + (n != null ? ' — ' + n + ' devices answered.' : '.'));
    } else if (s === 200 && st && st.state === 'error') {
      clearInterval(timer);
      fail('Scan failed: ' + (st.error || 'unknown'));
    } else if (waited > 120) {
      clearInterval(timer);
      fail('No result after 2 minutes — check the monitor service.');
    }
  }, 2000);
};
document.getElementById('dSave').onclick = async () => {
  const msg = document.getElementById('dMsg');
  clearMsg(msg);
  const devices = {};
  for (const tr of document.querySelectorAll('#dTable tr.d-row')) {
    const macEl = tr.querySelector('.d-mac');
    const mac = (tr.dataset.mac || (macEl ? macEl.value : '')).trim();
    const name = tr.querySelector('.d-name').value.trim();
    const type = tr.querySelector('.d-type').value;
    const watch = tr.querySelector('.d-watch').checked;
    // census rows with nothing filled in are just "seen devices" — only
    // rows carrying a name/type/watch become devices.json entries
    if (!mac && !name) continue;
    if (!name && !type && !watch) continue;
    // compact form: plain string unless IoT fields are set (the server
    // normalizes the same way — keeps untouched entries byte-identical)
    if (type || watch) {
      const v = {name};
      if (type) v.type = type;
      if (watch) v.watch = true;
      devices[mac] = v;
    } else {
      devices[mac] = name;
    }
  }
  const {status, data} = await api('/api/config', {devices});
  if (status === 200) {
    S.devices = devices;
    clearDirty('devices');
    showMsg(msg, 'ok', 'Saved. Device names refresh on the next scan cycle (within ~5 min).',
      (data.warnings || []).map(fmtIssue));
  } else {
    showMsg(msg, 'error', 'Not saved — please fix:', (data && data.errors || []).map(fmtIssue));
  }
};

load();

// restore the tab named in the URL hash (written by activateTab) so a
// refresh inside e.g. Devices lands back on Devices, not General
(function() {
  const name = (location.hash || '').replace('#', '');
  const btn = document.getElementById('tabbtn-' + name);
  if (btn) activateTab(btn);
})();
</script>
</div></body></html>
""")
