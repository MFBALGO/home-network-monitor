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
  h1 { font-size: 21px; margin: 0 0 4px; letter-spacing: 0.3px; }
  h2 { font-size: 15px; margin: 26px 0 10px; color: var(--text-secondary);
    text-transform: uppercase; letter-spacing: 1.2px; font-weight: 600; }
  .sub { color: var(--muted); font-size: 12.5px; font-family: var(--font-mono); margin-bottom: 22px; }
  .sub a { color: var(--accent); text-decoration: none; }
  .card { background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 12px; padding: 18px 20px; margin-bottom: 16px; }
  label { display: block; font-size: 12px; color: var(--text-secondary); margin: 12px 0 4px; }
  input[type=text], input[type=number], select {
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
    letter-spacing: 0.8px; padding: 6px 8px; border-bottom: 1px solid var(--border); }
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
  .steps { display: flex; gap: 6px; margin-bottom: 20px; font-family: var(--font-mono); font-size: 11px; }
  .steps span { flex: 1; text-align: center; padding: 5px 2px; border-radius: 6px;
    background: var(--surface-1); border: 1px solid var(--border-soft); color: var(--muted); }
  .steps span.active { border-color: var(--accent); color: var(--accent); }
  .steps span.done { color: var(--status-good); border-color: var(--border); }
  details { margin-top: 14px; }
  summary { cursor: pointer; color: var(--text-secondary); font-size: 13px; }
  .footer { margin-top: 30px; color: var(--muted); font-size: 11.5px; font-family: var(--font-mono); text-align: center; }
  .tabs { display: flex; gap: 8px; margin-bottom: 18px; }
  .tabs button { border-radius: 8px 8px 0 0; border-bottom: 2px solid transparent; }
  .tabs button.active { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
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
  .thresh-grid { display: grid; grid-template-columns: 110px 1fr 1fr; gap: 8px 12px; align-items: center; }
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

<div class="tabs">
  <button data-tab="general" class="active">General</button>
  <button data-tab="routers">Routers</button>
  <button data-tab="devices">Devices</button>
</div>

<div id="tab-general">
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
    <label>Hide devices whose IP starts with (comma-separated, e.g. <span class="mono">192.168.100.</span>)</label>
    <input type="text" id="gHide" placeholder="leave empty to show everything">
    <label>Internet plan speeds, Mbps (drawn on the speed chart — leave empty to skip)</label>
    <div class="row">
      <input type="number" id="gPlanDown" placeholder="download" min="1" style="width:130px">
      <input type="number" id="gPlanUp" placeholder="upload" min="1" style="width:130px">
    </div>
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
  <div class="msg" id="gMsg"></div>
  <div class="row" style="justify-content:flex-end"><button class="primary" id="gSave">Save general settings</button></div>
</div>

<div id="tab-routers" style="display:none">
  <div class="card">
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

<div id="tab-devices" style="display:none">
  <div class="card">
    <div class="sub" style="margin-bottom:10px">Friendly names shown on the dashboard's device
    table and in new-device alerts, keyed by MAC address.</div>
    <table id="dTable"><tr><th>MAC address</th><th>Name</th><th></th></tr></table>
    <div class="row" style="margin-top:10px"><button class="small" id="dAdd">+ Add device</button></div>
  </div>
  <div class="msg" id="dMsg"></div>
  <div class="row" style="justify-content:flex-end"><button class="primary" id="dSave">Save device names</button></div>
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
  ['uptime', 'Uptime %', 99.9, 99],
  ['wifi', 'Wi-Fi dBm', -60, -70],
];
const S = { config: {}, routers: [], devices: {} };
S.floorState = { floors: [], groundIndex: 0, main: null, onchange: refreshRouterFloorSelects };

// ---- tabs ----
document.querySelector('.tabs').onclick = (e) => {
  const b = e.target.closest('button'); if (!b) return;
  for (const x of document.querySelectorAll('.tabs button')) x.className = x === b ? 'active' : '';
  for (const name of ['general','routers','devices']) {
    document.getElementById('tab-' + name).style.display = name === b.dataset.tab ? '' : 'none';
  }
};

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

  document.getElementById('gTitle').value = S.config.title || 'Home Network Monitor';
  S.floorState.floors = (S.config.floors || []).slice();
  S.floorState.groundIndex = groundIndexFrom(S.floorState.floors, S.config.underground_floors);
  S.floorState.main = S.config.main_router_floor || null;
  renderFloors(S.floorState, 'gFloors');
  document.getElementById('gHide').value = (S.config.hide_ip_prefixes || []).join(', ');
  document.getElementById('gPlanDown').value = S.config.plan_down_mbps || '';
  document.getElementById('gPlanUp').value = S.config.plan_up_mbps || '';
  document.getElementById('gUpdateCheck').checked = S.config.update_check !== false;

  const grid = document.getElementById('gThresh');
  const th = S.config.thresholds || {};
  for (const [key, lbl, defGood, defFair] of THRESH_METRICS) {
    grid.insertAdjacentHTML('beforeend',
      '<span>' + lbl + '</span>' +
      '<input type="number" step="any" id="th-' + key + '-good" placeholder="' + defGood + '" value="' + (th[key] && th[key].good != null ? th[key].good : '') + '">' +
      '<input type="number" step="any" id="th-' + key + '-fair" placeholder="' + defFair + '" value="' + (th[key] && th[key].fair != null ? th[key].fair : '') + '">');
  }

  renderRouters();
  renderDevices();
}

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
  const hide = document.getElementById('gHide').value.split(',').map(s => s.trim()).filter(Boolean);
  if (hide.length) cfg.hide_ip_prefixes = hide; else delete cfg.hide_ip_prefixes;
  const pd = parseFloat(document.getElementById('gPlanDown').value);
  const pu = parseFloat(document.getElementById('gPlanUp').value);
  if (pd > 0) cfg.plan_down_mbps = pd; else delete cfg.plan_down_mbps;
  if (pu > 0) cfg.plan_up_mbps = pu; else delete cfg.plan_up_mbps;
  if (document.getElementById('gUpdateCheck').checked) delete cfg.update_check;
  else cfg.update_check = false;
  const th = {};
  for (const [key] of THRESH_METRICS) {
    const good = parseFloat(document.getElementById('th-' + key + '-good').value);
    const fair = parseFloat(document.getElementById('th-' + key + '-fair').value);
    const entry = {};
    if (!isNaN(good)) entry.good = good;
    if (!isNaN(fair)) entry.fair = fair;
    if (Object.keys(entry).length) th[key] = entry;
  }
  if (Object.keys(th).length) cfg.thresholds = th; else delete cfg.thresholds;

  const {status, data} = await api('/api/config', {config: cfg});
  if (status === 200) {
    S.config = cfg;
    showMsg(msg, 'ok', 'Saved. The dashboard picks this up on its next refresh (~1 min).',
      (data.warnings || []).map(fmtIssue));
  } else {
    showMsg(msg, 'error', 'Not saved — please fix:', (data && data.errors || []).map(fmtIssue));
  }
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
  const t = document.getElementById('rTable');
  t.innerHTML = '<tr><th>Name</th><th>IP</th><th>Floor</th><th></th></tr>' +
    S.routers.map(routerRow).join('');
}
function refreshRouterFloorSelects() {
  for (const sel of document.querySelectorAll('.r-floor')) {
    const cur = sel.value;
    sel.innerHTML = settingsFloorOptions(cur);
  }
}
function collectRouters() {
  const out = [];
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
function deviceRow(mac, name) {
  return '<tr>' +
    '<td><input type="text" class="d-mac mono" placeholder="aa:bb:cc:dd:ee:ff" value="' + esc(mac) + '"></td>' +
    '<td><input type="text" class="d-name" value="' + esc(name) + '"></td>' +
    '<td><button class="small danger d-del">&#10005;</button></td></tr>';
}
function renderDevices() {
  document.getElementById('dTable').innerHTML =
    '<tr><th>MAC address</th><th>Name</th><th></th></tr>' +
    Object.entries(S.devices).map(([m, n]) => deviceRow(m, n)).join('');
}
document.getElementById('dTable').onclick = (e) => {
  if (e.target.classList.contains('d-del')) e.target.closest('tr').remove();
};
document.getElementById('dAdd').onclick = () => {
  document.getElementById('dTable').insertAdjacentHTML('beforeend', deviceRow('', ''));
};
document.getElementById('dSave').onclick = async () => {
  const msg = document.getElementById('dMsg');
  clearMsg(msg);
  const devices = {};
  for (const tr of document.querySelectorAll('#dTable tr')) {
    const mac = tr.querySelector('.d-mac');
    if (!mac) continue;
    if (mac.value.trim() || tr.querySelector('.d-name').value.trim()) {
      devices[mac.value.trim()] = tr.querySelector('.d-name').value.trim();
    }
  }
  const {status, data} = await api('/api/config', {devices});
  if (status === 200) {
    S.devices = devices;
    showMsg(msg, 'ok', 'Saved. Device names refresh on the next scan cycle (within ~5 min).',
      (data.warnings || []).map(fmtIssue));
  } else {
    showMsg(msg, 'error', 'Not saved — please fix:', (data && data.errors || []).map(fmtIssue));
  }
};

load();
</script>
</div></body></html>
""")
