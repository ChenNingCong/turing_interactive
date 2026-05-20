// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, isError = false) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show' + (isError ? ' error' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = ''; }, 3500);
}

// ── Sessions ──────────────────────────────────────────────────────────────────
function statusBadge(status) {
  const cls = status === 'READY'   ? 'badge-ready'   :
              status === 'PENDING' ? 'badge-pending' :
              status === 'RUNNING' ? 'badge-running' : 'badge-other';
  return `<span class="badge ${cls}">${status}</span>`;
}

async function refreshSessions() {
  document.getElementById('refresh-status').textContent = 'Refreshing…';
  try {
    const r  = await fetch('/api/sessions');
    const sessions = await r.json();
    renderSessions(sessions);
    const now = new Date().toLocaleTimeString();
    document.getElementById('refresh-status').textContent = `Updated ${now}`;
  } catch {
    toast('Failed to fetch sessions', true);
    document.getElementById('refresh-status').textContent = 'Error';
  }
}

function renderSessions(sessions) {
  const tbody = document.getElementById('sessions-tbody');
  const count = document.getElementById('session-count');
  count.textContent = sessions.length ? `${sessions.length} job${sessions.length !== 1 ? 's' : ''}` : '';

  if (!sessions.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="9">No active sessions. Submit a job below.</td></tr>';
    return;
  }

  tbody.innerHTML = sessions.map(s => {
    const sshEsc = (s.ssh_cmd || '').replace(/'/g, "\\'");
    const alias = s.ssh_cmd && s.node ? `turing-${s.node}-${s.jid}` : null;
    const sshCell = s.ssh_cmd
      ? `<span class="ssh-cell" title="${s.ssh_cmd}">${s.ssh_cmd}</span>
         <span style="font-size:11px;color:var(--muted);font-family:var(--mono)">alias: ${alias}</span>`
      : `<span class="text-muted">waiting for sshd…</span>`;
    const copyBtn = s.ssh_cmd
      ? `<button class="btn btn-copy btn-small" onclick="copySSH('${sshEsc}')">Copy</button>` : '';
    const termBtn = s.ssh_cmd
      ? `<button class="btn btn-primary btn-small" onclick="openTerminal('${s.jid}','${s.node || ''}')">Terminal</button>` : '';
    const startCell = s.start_time && s.start_time !== 'N/A' && s.start_time !== 'Unknown'
      ? `<span style="font-family:var(--mono);font-size:11px">${s.start_time}</span>`
      : `<span class="text-muted">—</span>`;
    return `<tr>
      <td style="font-family:var(--mono)">${s.jid}</td>
      <td>${statusBadge(s.status)}</td>
      <td style="font-family:var(--mono);font-size:12px">${s.node || '—'}</td>
      <td>${s.partition || '—'}</td>
      <td>${s.timelimit || '—'}</td>
      <td style="font-family:var(--mono);font-size:12px">${s.priority || '—'}</td>
      <td>${startCell}</td>
      <td style="display:flex;gap:4px;white-space:nowrap">
        ${termBtn}
        ${copyBtn}
        <button class="btn btn-danger btn-small" onclick="confirmKill('${s.jid}')">Kill</button>
      </td>
      <td>${sshCell}</td>
    </tr>`;
  }).join('');
}

let _killJid = null;

function confirmKill(jid) {
  _killJid = jid;
  document.getElementById('kill-msg').textContent = `Are you sure you want to cancel job ${jid}?`;
  document.getElementById('kill-modal').style.display = 'flex';
  document.getElementById('kill-confirm-btn').focus();
}

function closeKill() {
  _killJid = null;
  document.getElementById('kill-modal').style.display = 'none';
}

async function doKillConfirmed() {
  const jid = _killJid;
  closeKill();
  const r = await fetch(`/api/kill/${jid}`, { method: 'POST' });
  const d = await r.json();
  if (d.ok) { toast(`Job ${jid} cancelled`); refreshSessions(); }
  else       { toast(d.error || 'Kill failed', true); }
}

function copySSH(cmd) {
  navigator.clipboard.writeText(cmd).then(() => toast('SSH command copied!'));
}

// ── Job form ──────────────────────────────────────────────────────────────────
function getFormValues() {
  return {
    PARTITION: document.getElementById('f-partition').value.trim(),
    REQCPU:    parseInt(document.getElementById('f-cpu').value),
    REQMEM:    parseInt(document.getElementById('f-mem').value) * 1024,
    REQTIME:   parseInt(document.getElementById('f-time').value) * 60,
    REQGPU:    parseInt(document.getElementById('f-gpu').value),
    REQTYP:    document.getElementById('f-gputype').value.trim(),
    nodelist:  document.getElementById('f-nodelist').value.trim(),
    account:   document.getElementById('f-account').value.trim(),
  };
}

function setFormValues(cfg) {
  document.getElementById('f-partition').value = cfg.PARTITION || '';
  document.getElementById('f-cpu').value       = cfg.REQCPU   ?? 8;
  document.getElementById('f-mem').value       = Math.round((cfg.REQMEM ?? 16384) / 1024);
  document.getElementById('f-time').value      = Math.round((cfg.REQTIME ?? 1440) / 60);
  document.getElementById('f-gpu').value       = cfg.REQGPU   ?? 0;
  const gpuSel = document.getElementById('f-gputype');
  gpuSel.value = cfg.REQTYP || 'H100';
  if (!gpuSel.value) gpuSel.selectedIndex = 0;  // fallback if type not in list
  document.getElementById('f-nodelist').value  = cfg.nodelist || '';
  document.getElementById('f-account').value   = cfg.account  || '';
  updateGpuTypeState();
}

function updateGpuTypeState() {
  const gpus = parseInt(document.getElementById('f-gpu').value);
  document.getElementById('f-gputype').disabled = (gpus === 0);
}

async function submitJob() {
  const cfg = getFormValues();
  const btn = document.getElementById('submit-btn');
  const msg = document.getElementById('submit-msg');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Submitting…';
  msg.textContent = '';
  try {
    const r = await fetch('/api/allocate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    });
    const d = await r.json();
    if (d.ok) {
      toast(`Job ${d.job_id} submitted (port ${d.port})`);
      msg.textContent = `✓ Job ${d.job_id} queued`;
      refreshSessions();
    } else {
      toast(d.error || 'Submission failed', true);
      msg.textContent = d.error || 'Error';
    }
  } catch {
    toast('Network error', true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '&#9654; Submit Job';
  }
}

// ── Templates ─────────────────────────────────────────────────────────────────
let templates = [];

async function refreshTemplates() {
  const r = await fetch('/api/templates');
  templates = await r.json();
  renderTemplates();
}

function tmplSummary(cfg) {
  return [
    cfg.PARTITION,
    cfg.REQCPU  ? `${cfg.REQCPU}c`  : null,
    cfg.REQMEM  ? `${Math.round(cfg.REQMEM / 1024)}G` : null,
    cfg.REQGPU  ? `${cfg.REQGPU}×${cfg.REQTYP || 'GPU'}` : 'CPU-only',
  ].filter(Boolean).join(' · ');
}

function renderTemplates() {
  const el = document.getElementById('tmpl-list');
  if (!templates.length) {
    el.innerHTML = '<span class="text-muted" style="font-size:12px;padding:8px 0;">No templates yet.</span>';
    return;
  }
  el.innerHTML = templates.map((t, i) => `
    <div class="tmpl-item" id="tmpl-${i}" onclick="loadTemplate(${i})">
      <div style="flex:1;min-width:0;">
        <div class="tmpl-name">${t.name}</div>
        <div class="tmpl-meta">${tmplSummary(t.config)}</div>
      </div>
      <button class="btn btn-danger btn-small"
              onclick="event.stopPropagation(); deleteTemplate('${t.name}')">&#215;</button>
    </div>
  `).join('');
}

function loadTemplate(i) {
  setFormValues(templates[i].config);
  document.querySelectorAll('.tmpl-item').forEach(el => el.classList.remove('active'));
  document.getElementById(`tmpl-${i}`)?.classList.add('active');
  toast(`Loaded: ${templates[i].name}`);
}

async function deleteTemplate(name) {
  if (!confirm(`Delete template "${name}"?`)) return;
  const r = await fetch(`/api/templates/${encodeURIComponent(name)}`, { method: 'DELETE' });
  const d = await r.json();
  if (d.ok) { toast(`Deleted ${name}`); refreshTemplates(); }
  else       { toast(d.error || 'Delete failed', true); }
}


async function saveAsTemplate() {
  const name = prompt('Template name (no extension needed):');
  if (!name) return;
  const r = await fetch('/api/templates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, config: getFormValues() }),
  });
  const d = await r.json();
  if (d.ok) { toast(`Saved: ${d.name}`); refreshTemplates(); }
  else       { toast(d.error || 'Save failed', true); }
}

// ── Preview ───────────────────────────────────────────────────────────────────
async function previewScript() {
  const cfg = getFormValues();
  const r = await fetch('/api/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  });
  const d = await r.json();
  document.getElementById('preview-content').textContent = d.script || d.error;
  document.getElementById('preview-modal').style.display = 'flex';
}

function closePreview() {
  document.getElementById('preview-modal').style.display = 'none';
}

// ── GPU types ─────────────────────────────────────────────────────────────────
async function loadGpuTypes() {
  const sel = document.getElementById('f-gputype');
  try {
    const r = await fetch('/api/gpu_types');
    const types = await r.json();
    if (types.length) {
      sel.innerHTML = types.map(t => `<option value="${t}">${t}</option>`).join('');
    } else {
      sel.innerHTML = '<option value="">None available</option>';
    }
  } catch {
    sel.innerHTML = '<option value="">Unavailable</option>';
  }
}

// ── Fairshare ─────────────────────────────────────────────────────────────────
async function loadFairshare() {
  const el = document.getElementById('fairshare-body');
  el.innerHTML = '<span class="text-muted">Loading…</span>';
  try {
    const r = await fetch('/api/fairshare');
    const d = await r.json();
    const accountSel = document.getElementById('f-account');
    if (!d.ok || !d.rows.length) {
      el.innerHTML = '<span class="text-muted">No data available.</span>';
      accountSel.innerHTML = '<option value="">None</option>';
      return;
    }
    const prev = accountSel.value;
    accountSel.innerHTML = '<option value="">(none)</option>' +
      d.rows.map(r => {
        const pct = Math.round(parseFloat(r.fairshare) * 100);
        const parts = r.partitions.length ? ` [${r.partitions.join(', ')}]` : '';
        return `<option value="${r.account}" data-default-partition="${r.default_partition || ''}">${r.account}${parts} — fairshare ${isNaN(pct) ? '?' : pct + '%'}</option>`;
      }).join('');
    if (prev) accountSel.value = prev;
    el.innerHTML = d.rows.map(row => {
      const fs = parseFloat(row.fairshare);
      const pct = isNaN(fs) ? null : Math.round(fs * 100);
      const barColor = isNaN(fs) ? 'var(--muted)' :
                       fs >= 0.6 ? 'var(--green)' :
                       fs >= 0.3 ? 'var(--yellow)' : 'var(--red)';
      const bar = pct !== null ? `
        <div style="background:#0d0d1e;border-radius:3px;height:6px;margin-top:4px;overflow:hidden">
          <div style="width:${pct}%;height:100%;background:${barColor};transition:width 0.4s"></div>
        </div>` : '';
      return `
        <div style="margin-bottom:10px;padding:8px;background:#0d0d1e;border-radius:var(--radius);border:1px solid #2a2a4a">
          <div style="display:flex;justify-content:space-between;align-items:baseline">
            <span style="font-family:var(--mono);color:var(--text)">${row.account || '(root)'}</span>
            <span style="color:${barColor};font-weight:bold">${pct !== null ? pct + '%' : row.fairshare}</span>
          </div>
          ${bar}
          <div style="color:var(--muted);font-size:10px;margin-top:4px">effective usage: ${row.effec_usage || '—'}</div>
          ${row.partitions && row.partitions.length ? `<div style="margin-top:5px;display:flex;flex-wrap:wrap;gap:3px">${row.partitions.map(p => `<span style="font-family:var(--mono);font-size:10px;padding:1px 5px;border-radius:3px;background:#0d0d1e;border:1px solid ${p === row.default_partition ? 'var(--accent)' : '#2a2a4a'};color:${p === row.default_partition ? 'var(--accent)' : 'var(--muted)'}">${p}${p === row.default_partition ? ' ✓' : ''}</span>`).join('')}</div>` : ''}
        </div>`;
    }).join('');
  } catch {
    el.innerHTML = '<span class="text-muted">Failed to load.</span>';
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  updateGpuTypeState();
  loadGpuTypes();
  refreshSessions();
  refreshTemplates();
  loadFairshare();
  setInterval(refreshSessions, 15000);

  document.getElementById('f-account').addEventListener('change', function() {
    const opt = this.options[this.selectedIndex];
    const defPart = opt?.dataset?.defaultPartition;
    if (defPart) document.getElementById('f-partition').value = defPart;
  });
});

/* ───────────────────────────────────────────────────────────────────────── *
 * In-browser terminal (xterm.js ↔ /ws/ssh/<jid>)
 * Short-lived SSH per WebSocket connection. Close the modal → SSH dies.
 * Run tmux/screen inside the shell yourself for persistence.
 * ───────────────────────────────────────────────────────────────────────── */

/* Tab manager — Jupyter-style: tab "control" is always present and uncloseable;
 * each opened terminal becomes its own tab+panel. */

const _tabs = new Map();   // tabId -> {jid, node, term, fit, ws}
let   _activeTab = 'control';

function _tabIdForJid(jid) { return 't-' + jid; }

function switchTab(tabId) {
  document.querySelectorAll('#tab-bar .tab').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === tabId));
  document.querySelectorAll('#tab-panels .tab-panel').forEach(p =>
    p.classList.toggle('active', p.id === 'tabp-' + tabId));
  _activeTab = tabId;
  // After becoming visible, the xterm needs a fit() so it picks up its real size.
  const entry = _tabs.get(tabId);
  if (entry) {
    requestAnimationFrame(() => {
      try { entry.fit.fit(); } catch(e) {}
      try { entry.term.focus(); } catch(e) {}
    });
  }
}

function openTerminal(jid, node) {
  const tabId = _tabIdForJid(jid);
  // Already open? Just switch.
  if (_tabs.has(tabId)) {
    switchTab(tabId);
    return;
  }

  // Tab button
  const tabBar = document.getElementById('tab-bar');
  const btn = document.createElement('button');
  btn.className = 'tab';
  btn.dataset.tab = tabId;
  btn.onclick = () => switchTab(tabId);
  const label = document.createElement('span');
  label.textContent = `${node || '?'} · ${jid}`;
  const close = document.createElement('span');
  close.className = 'tab-close';
  close.textContent = '×';
  close.title = 'Close terminal';
  close.onclick = (e) => { e.stopPropagation(); closeTerminalTab(tabId); };
  btn.appendChild(label);
  btn.appendChild(close);
  tabBar.appendChild(btn);

  // Panel + host
  const panels = document.getElementById('tab-panels');
  const panel = document.createElement('div');
  panel.className = 'tab-panel term-panel';
  panel.id = 'tabp-' + tabId;
  const status = document.createElement('div');
  status.className = 'term-status';
  status.textContent = 'connecting…';
  const host = document.createElement('div');
  host.className = 'term-host';
  panel.appendChild(status);
  panel.appendChild(host);
  panels.appendChild(panel);

  // xterm.js instance
  const term = new Terminal({
    cursorBlink: true,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
    fontSize: 13,
    theme: { background: '#0d0d1f' },
    scrollback: 5000,
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(host);

  // WebSocket
  const scheme = (location.protocol === 'https:') ? 'wss' : 'ws';
  const ws = new WebSocket(`${scheme}://${location.host}/ws/ssh/${jid}`);

  ws.onopen = () => {
    status.textContent = 'connected';
    _sendResize(tabId);
    term.focus();
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string' && ev.data.startsWith('{"type":"error"')) {
      try {
        const o = JSON.parse(ev.data);
        if (o.type === 'error') {
          term.write(`\r\n\x1b[31m[error] ${o.msg}\x1b[0m\r\n`);
          status.textContent = 'error';
          return;
        }
      } catch (e) {/* fall through */}
    }
    term.write(ev.data);
  };
  ws.onclose = () => {
    status.textContent = 'disconnected';
    try { term.write('\r\n\x1b[33m[connection closed — close this tab to dismiss]\x1b[0m\r\n'); } catch(e){}
  };
  ws.onerror = () => { status.textContent = 'ws error'; };

  term.onData(d => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'data', payload: d }));
    }
  });
  term.onResize(({ cols, rows }) => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'resize', cols, rows }));
    }
  });

  _tabs.set(tabId, { jid, node, term, fit, ws });

  switchTab(tabId);
}

function _sendResize(tabId) {
  const e = _tabs.get(tabId);
  if (!e || e.ws.readyState !== WebSocket.OPEN) return;
  e.ws.send(JSON.stringify({ type: 'resize', cols: e.term.cols, rows: e.term.rows }));
}

function closeTerminalTab(tabId) {
  const e = _tabs.get(tabId);
  if (!e) return;
  try { e.ws.close(); } catch(_){}
  try { e.term.dispose(); } catch(_){}
  document.querySelector(`#tab-bar .tab[data-tab="${tabId}"]`)?.remove();
  document.getElementById('tabp-' + tabId)?.remove();
  _tabs.delete(tabId);
  if (_activeTab === tabId) switchTab('control');
}

// Re-fit the active terminal when the window resizes.
window.addEventListener('resize', () => {
  const e = _tabs.get(_activeTab);
  if (e) { try { e.fit.fit(); } catch(_){} }
});
