// Native Tauri launcher for turing_interactive.
//
// The UI is intentionally vanilla JS (no build step). All cluster interactions
// go through Tauri commands defined in src-tauri/src/lib.rs. We treat the
// backend as a thin wrapper around `ssh login-host -- bash -lc "<remote>"`,
// so anything the launcher can't already do, you can replicate by hand.

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const $ = (id) => document.getElementById(id);

// ---------- toast ----------
let _toastT;
function toast(msg, kind) {
  const el = $("toast");
  el.textContent = msg;
  el.className = kind === "error" ? "show error" : "show";
  clearTimeout(_toastT);
  _toastT = setTimeout(() => el.classList.remove("show"), 2400);
}

// ---------- tabs ----------
function switchTab(name) {
  document.querySelectorAll("#tabs .tab").forEach(t =>
    t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach(p =>
    p.classList.toggle("active", p.id === `panel-${name}`));
}
document.querySelectorAll("#tabs .tab").forEach(t =>
  t.addEventListener("click", () => switchTab(t.dataset.tab)));

// ---------- log pane ----------
const logPane = $("log-pane");
function appendLog(line) {
  const atBottom = logPane.scrollTop + logPane.clientHeight + 8 >= logPane.scrollHeight;
  const ts = new Date().toLocaleTimeString();
  logPane.textContent += `[${ts}] ${line}\n`;
  if (atBottom) logPane.scrollTop = logPane.scrollHeight;
}
$("btn-clear-logs").addEventListener("click", () => { logPane.textContent = ""; });
listen("log", e => appendLog(e.payload));

// ---------- config (settings modal) ----------
let _config = { host: "", repo: "~/turing_interactive" };

function updateHostPill() {
  const p = $("host-pill");
  if (!_config.host) {
    p.textContent = "no host configured";
    p.className = "pill pill-muted";
  } else {
    p.textContent = _config.host;
    p.className = "pill pill-ok";
  }
}

async function openSettings() {
  // refresh host list
  const hosts = await invoke("list_ssh_hosts");
  const sel = $("cfg-host");
  sel.innerHTML = "";
  const blank = document.createElement("option");
  blank.value = ""; blank.textContent = "(none)";
  sel.appendChild(blank);
  for (const h of hosts) {
    const o = document.createElement("option");
    o.value = h.name;
    o.textContent = h.hostname ? `${h.name}  →  ${h.hostname}` : h.name;
    sel.appendChild(o);
  }
  sel.value = _config.host || "";
  $("cfg-repo").value = _config.repo || "~/turing_interactive";
  $("modal-settings").classList.remove("hidden");
}
function closeSettings() { $("modal-settings").classList.add("hidden"); }

$("btn-settings").addEventListener("click", openSettings);
$("cfg-cancel").addEventListener("click", closeSettings);
$("cfg-save").addEventListener("click", async () => {
  const cfg = { host: $("cfg-host").value, repo: $("cfg-repo").value.trim() || "~/turing_interactive" };
  await invoke("save_config", { cfg });
  _config = cfg;
  updateHostPill();
  closeSettings();
  toast("saved");
  await refreshAll();
});

// ---------- sessions ----------
function statusClass(s) {
  if (!s) return "badge-other";
  s = s.toUpperCase();
  if (s === "RUNNING") return "badge-running";
  if (s === "PENDING") return "badge-pending";
  if (s === "COMPLETED" || s === "READY") return "badge-ready";
  if (s === "FAILED" || s === "CANCELLED" || s === "TIMEOUT") return "badge-error";
  return "badge-other";
}

function renderSessions(sessions) {
  const tbody = $("sessions-tbody");
  if (!sessions.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="8">No sessions. Submit one above.</td></tr>`;
    return;
  }
  tbody.innerHTML = "";
  for (const s of sessions) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${s.jid}</td>
      <td><span class="badge ${statusClass(s.status)}">${s.status || "—"}</span></td>
      <td>${s.node || "—"}</td>
      <td>${s.partition || "—"}</td>
      <td>${s.time_limit || "—"}</td>
      <td>${s.priority || "—"}</td>
      <td class="row-actions">
        <button class="btn btn-copy btn-small" data-copy="${encodeURIComponent(s.ssh_cmd)}" ${s.ssh_cmd ? "" : "disabled"}>Copy SSH</button>
        <button class="btn btn-primary btn-small" data-forward='${JSON.stringify(s).replace(/'/g, "&#39;")}' ${s.ssh_cmd ? "" : "disabled"}>Forward…</button>
        <button class="btn btn-danger btn-small" data-cancel="${s.jid}">Cancel</button>
      </td>
      <td class="ssh-cell" title="${escapeAttr(s.ssh_cmd)}">${s.ssh_cmd || "(not ready)"}</td>
    `;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", e => {
    const txt = decodeURIComponent(e.currentTarget.dataset.copy);
    navigator.clipboard.writeText(txt);
    toast("ssh command copied");
  }));
  tbody.querySelectorAll("[data-cancel]").forEach(b => b.addEventListener("click", async e => {
    const jid = e.currentTarget.dataset.cancel;
    if (!confirm(`Cancel job ${jid}?`)) return;
    try {
      await invoke("cancel_session", { jid });
      toast(`cancelled ${jid}`);
      refreshSessions();
    } catch (err) { toast(err, "error"); }
  }));
  tbody.querySelectorAll("[data-forward]").forEach(b => b.addEventListener("click", e => {
    const s = JSON.parse(e.currentTarget.dataset.forward.replace(/&#39;/g, "'"));
    openForwardModalFromSession(s);
  }));
}

function escapeAttr(s) {
  return (s || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

async function refreshSessions() {
  if (!_config.host) return;
  try {
    const sessions = await invoke("list_sessions");
    renderSessions(sessions);
  } catch (err) {
    appendLog(`list_sessions error: ${err}`);
    toast(err, "error");
  }
}

async function refreshTemplates() {
  if (!_config.host) return;
  try {
    const tmpls = await invoke("list_templates");
    const sel = $("tpl-select");
    sel.innerHTML = "";
    if (!tmpls.length) {
      const o = document.createElement("option");
      o.textContent = "(no templates)"; o.disabled = true;
      sel.appendChild(o);
      return;
    }
    for (const t of tmpls) {
      const o = document.createElement("option");
      o.value = t; o.textContent = t;
      sel.appendChild(o);
    }
  } catch (err) {
    appendLog(`list_templates error: ${err}`);
  }
}

$("btn-refresh").addEventListener("click", refreshAll);
$("btn-submit").addEventListener("click", async () => {
  const tpl = $("tpl-select").value;
  if (!tpl) return;
  const btn = $("btn-submit");
  btn.disabled = true; btn.textContent = "submitting…";
  try {
    const jid = await invoke("submit_session", { args: { template: tpl } });
    toast(`submitted job ${jid}`);
    await refreshSessions();
  } catch (err) {
    toast(err, "error");
    appendLog(`submit error: ${err}`);
  } finally {
    btn.disabled = false; btn.textContent = "Submit";
  }
});

// ---------- forwards ----------
async function refreshForwards() {
  const list = await invoke("list_forwards");
  const tbody = $("forwards-tbody");
  if (!list.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="5">No active forwards.</td></tr>`;
    return;
  }
  tbody.innerHTML = "";
  for (const f of list) {
    const tr = document.createElement("tr");
    const statusCls = f.status === "up" ? "badge-running" : f.status === "starting" ? "badge-pending" : "badge-other";
    tr.innerHTML = `
      <td><a href="#" data-open="${f.local_port}">localhost:${f.local_port}</a></td>
      <td>${f.remote_host}:${f.remote_port}</td>
      <td>${f.host}</td>
      <td><span class="badge ${statusCls}">${f.status}</span></td>
      <td class="row-actions">
        <button class="btn btn-danger btn-small" data-stop="${f.id}">Stop</button>
      </td>
    `;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll("[data-stop]").forEach(b => b.addEventListener("click", async e => {
    const id = e.currentTarget.dataset.stop;
    try { await invoke("remove_forward", { id }); refreshForwards(); }
    catch (err) { toast(err, "error"); }
  }));
  tbody.querySelectorAll("[data-open]").forEach(a => a.addEventListener("click", async e => {
    e.preventDefault();
    const port = e.currentTarget.dataset.open;
    try { await invoke("open_url", { url: `http://localhost:${port}` }); }
    catch (err) { toast(err, "error"); }
  }));
}

listen("forwards-changed", refreshForwards);
listen("forward-ended", e => appendLog(`forward ${e.payload} ended`));

// ---------- add-forward modal ----------
function openForwardModal(prefill) {
  $("fwd-host").value = prefill.host || _config.host || "";
  $("fwd-local").value = prefill.local_port || 8001;
  $("fwd-remote-port").value = prefill.remote_port || 8001;
  $("fwd-remote-host").value = prefill.remote_host || "localhost";
  $("fwd-extra").value = (prefill.extra_args || []).join(" ");
  $("fwd-title").textContent = prefill.title || "Add port forward";
  updateFwdPreview();
  $("modal-forward").classList.remove("hidden");
}
function closeForwardModal() { $("modal-forward").classList.add("hidden"); }

$("btn-add-forward").addEventListener("click", () => openForwardModal({}));
$("fwd-cancel").addEventListener("click", closeForwardModal);

async function updateFwdPreview() {
  const args = collectFwdArgs();
  try {
    const preview = await invoke("forward_command_preview", { args });
    $("fwd-preview").innerHTML = `would run: <code>${escapeAttr(preview)}</code>`;
  } catch { /* ignore */ }
}
["fwd-host", "fwd-local", "fwd-remote-port", "fwd-remote-host", "fwd-extra"]
  .forEach(id => $(id).addEventListener("input", updateFwdPreview));

function collectFwdArgs() {
  const extra = $("fwd-extra").value.trim();
  return {
    host: $("fwd-host").value.trim(),
    local_port: parseInt($("fwd-local").value, 10) || 0,
    remote_host: $("fwd-remote-host").value.trim() || "localhost",
    remote_port: parseInt($("fwd-remote-port").value, 10) || 0,
    extra_args: extra ? extra.split(/\s+/) : [],
    label: null,
  };
}

$("fwd-go").addEventListener("click", async () => {
  const args = collectFwdArgs();
  if (!args.host || !args.local_port || !args.remote_port) {
    toast("host + ports required", "error");
    return;
  }
  try {
    await invoke("add_forward", { args });
    closeForwardModal();
    toast("forward opened");
    switchTab("forwards");
    refreshForwards();
  } catch (err) {
    toast(err, "error");
  }
});

// ---------- "Forward..." from a session row ----------
// Parses the per-job ssh command (from server_<jid>.sh) and pre-fills the
// modal so the user only has to pick local/remote ports.
function openForwardModalFromSession(s) {
  const parsed = parseSshCommand(s.ssh_cmd);
  openForwardModal({
    title: `Forward port via job ${s.jid}`,
    host: parsed.host,
    extra_args: parsed.extra,
    remote_host: "localhost",
    remote_port: 8888,
    local_port: 8888,
  });
}

// Parse `ssh -p N -i /path/to/key user@host` into {host, extra:[...]}
function parseSshCommand(cmd) {
  if (!cmd) return { host: "", extra: [] };
  const toks = cmd.trim().split(/\s+/);
  if (toks[0] !== "ssh") return { host: toks[toks.length - 1] || "", extra: [] };
  const extra = [];
  let host = "";
  for (let i = 1; i < toks.length; i++) {
    const t = toks[i];
    if (t === "-p" || t === "-i" || t === "-o" || t === "-F") {
      extra.push(t);
      if (i + 1 < toks.length) extra.push(toks[++i]);
    } else if (t.startsWith("-")) {
      extra.push(t);   // single-token flag, e.g. -v
    } else {
      host = t;        // first non-flag is the host
      break;
    }
  }
  return { host, extra };
}

// ---------- bootstrap ----------
async function refreshAll() {
  await refreshTemplates();
  await refreshSessions();
  await refreshForwards();
}

(async function init() {
  _config = await invoke("load_config");
  updateHostPill();
  if (!_config.host) {
    openSettings();
    toast("pick a login host to get started");
  }
  await refreshAll();
})();
