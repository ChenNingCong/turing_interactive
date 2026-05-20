//! Native launcher backend.
//!
//! Talks to the cluster login node via plain `ssh` subprocesses (so the user's
//! existing `~/.ssh/config`, keys, and ssh-agent setup just work). Three concerns:
//!
//!   1. **Sessions** — list / submit / cancel SLURM jobs on the login node.
//!      Submission re-uses the repo's own `python sbatch_run.py`; we don't
//!      reimplement template logic here.
//!   2. **Forwards** — long-lived `ssh -N -L …` subprocesses; one per row.
//!   3. **Launcher config** — local file at `dirs::config_dir()/turing-launcher/`.
//!
//! Errors are returned as `Result<_, String>` so they land in the JS catch block
//! verbatim. Long-running things (forwards, submissions) emit `log` events.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Mutex, atomic::{AtomicU64, Ordering}};
use std::thread;
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager, State};

// ---------------------------------------------------------------------------
// App state
// ---------------------------------------------------------------------------

#[derive(Default)]
pub struct AppState {
    forwards: Mutex<HashMap<String, ForwardEntry>>,
    next_id: AtomicU64,
}

struct ForwardEntry {
    child: Child,
    info: ForwardInfo,
}

// ---------------------------------------------------------------------------
// Launcher config (persisted)
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Default, Clone)]
pub struct LauncherConfig {
    #[serde(default)]
    pub host: String,
    #[serde(default = "default_repo")]
    pub repo: String,
}

fn default_repo() -> String { "~/turing_interactive".to_string() }

fn config_path() -> PathBuf {
    let base = dirs::config_dir().unwrap_or_else(|| PathBuf::from("."));
    base.join("turing-launcher").join("config.json")
}

#[tauri::command]
fn load_config() -> LauncherConfig {
    let p = config_path();
    fs::read_to_string(&p)
        .ok()
        .and_then(|s| serde_json::from_str::<LauncherConfig>(&s).ok())
        .unwrap_or_default()
}

#[tauri::command]
fn save_config(cfg: LauncherConfig) -> Result<(), String> {
    let p = config_path();
    if let Some(parent) = p.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let s = serde_json::to_string_pretty(&cfg).map_err(|e| e.to_string())?;
    fs::write(&p, s).map_err(|e| e.to_string())?;
    Ok(())
}

// ---------------------------------------------------------------------------
// SSH config parser
// ---------------------------------------------------------------------------

#[derive(Serialize, Clone)]
pub struct SshHost {
    pub name: String,
    pub hostname: Option<String>,
    pub user: Option<String>,
}

fn ssh_config_path() -> Option<PathBuf> {
    dirs::home_dir().map(|h| h.join(".ssh").join("config"))
}

fn parse_ssh_config(text: &str) -> Vec<SshHost> {
    let mut out: Vec<SshHost> = Vec::new();
    let mut staged: Vec<SshHost> = Vec::new();

    let flush = |staged: &mut Vec<SshHost>, out: &mut Vec<SshHost>| {
        for h in staged.drain(..) {
            if !h.name.contains('*') && !h.name.contains('?') {
                out.push(h);
            }
        }
    };

    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') { continue; }
        let mut parts = line.splitn(2, |c: char| c.is_whitespace() || c == '=');
        let key = parts.next().unwrap_or("").to_lowercase();
        let rest = parts.next().unwrap_or("").trim_start_matches(|c: char| c.is_whitespace() || c == '=').trim();
        if rest.is_empty() { continue; }

        match key.as_str() {
            "host" => {
                flush(&mut staged, &mut out);
                for name in rest.split_whitespace() {
                    staged.push(SshHost { name: name.to_string(), hostname: None, user: None });
                }
            }
            "hostname" => for h in staged.iter_mut() { h.hostname = Some(rest.to_string()); }
            "user" => for h in staged.iter_mut() { h.user = Some(rest.to_string()); }
            _ => {}
        }
    }
    flush(&mut staged, &mut out);
    out
}

#[tauri::command]
fn list_ssh_hosts() -> Vec<SshHost> {
    let p = match ssh_config_path() { Some(p) => p, None => return vec![] };
    match fs::read_to_string(&p) {
        Ok(t) => parse_ssh_config(&t),
        Err(_) => vec![],
    }
}

// ---------------------------------------------------------------------------
// SSH command helpers
// ---------------------------------------------------------------------------

/// Run a one-shot `ssh host -- bash -lc "<remote>"` and return stdout.
/// Stderr is bubbled up if the exit code is non-zero.
fn ssh_run(host: &str, remote: &str) -> Result<String, String> {
    if host.is_empty() {
        return Err("no login host configured (open Settings)".into());
    }
    let out = Command::new("ssh")
        .arg("-o").arg("BatchMode=yes")
        .arg("-o").arg("ConnectTimeout=10")
        .arg(host)
        .arg("--")
        .arg("bash").arg("-lc").arg(remote)
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("could not spawn ssh: {e}"))?;
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr).into_owned();
        return Err(if stderr.is_empty() {
            format!("ssh exited with status {}", out.status)
        } else {
            stderr.trim().to_string()
        });
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------

#[derive(Serialize, Clone, Default)]
pub struct Session {
    pub jid: String,
    pub status: String,
    pub node: String,
    pub partition: String,
    pub time_limit: String,
    pub priority: String,
    pub name: String,
    pub ssh_cmd: String,   // contents of server_<jid>.sh, empty if not yet ready
}

const REMOTE_LIST_SESSIONS: &str = r#"
set -u
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/turing_interactive/jobs"
echo "===SQUEUE==="
squeue -h -u "$USER" -o "%i|%T|%N|%P|%L|%Q|%j" 2>/dev/null || true
echo "===SERVERS==="
if [ -d "$STATE_DIR" ]; then
    for f in "$STATE_DIR"/server_*.sh; do
        [ -r "$f" ] || continue
        jid=$(basename "$f" .sh | sed 's/^server_//')
        # collapse to single line — every published server file is one ssh command
        printf '%s|' "$jid"
        head -n 1 "$f"
    done
fi
"#;

#[tauri::command]
fn list_sessions(app: AppHandle) -> Result<Vec<Session>, String> {
    let cfg = load_config();
    let out = ssh_run(&cfg.host, REMOTE_LIST_SESSIONS)?;
    let _ = app.emit("log", format!("list_sessions ok ({} bytes)", out.len()));

    let mut sessions: HashMap<String, Session> = HashMap::new();
    let mut section = "";
    for line in out.lines() {
        let l = line.trim_end();
        if l == "===SQUEUE===" { section = "squeue"; continue; }
        if l == "===SERVERS===" { section = "servers"; continue; }
        if l.is_empty() { continue; }

        match section {
            "squeue" => {
                // jid|state|node|part|time_limit|prio|name
                let parts: Vec<&str> = l.splitn(7, '|').collect();
                if parts.len() < 7 { continue; }
                let s = Session {
                    jid: parts[0].to_string(),
                    status: parts[1].to_string(),
                    node: parts[2].to_string(),
                    partition: parts[3].to_string(),
                    time_limit: parts[4].to_string(),
                    priority: parts[5].to_string(),
                    name: parts[6].to_string(),
                    ssh_cmd: String::new(),
                };
                sessions.insert(s.jid.clone(), s);
            }
            "servers" => {
                // jid|<ssh command line>
                if let Some(i) = l.find('|') {
                    let (jid, rest) = l.split_at(i);
                    let cmd = rest.trim_start_matches('|').to_string();
                    sessions.entry(jid.to_string())
                        .or_insert_with(|| Session { jid: jid.to_string(), ..Default::default() })
                        .ssh_cmd = cmd;
                }
            }
            _ => {}
        }
    }
    let mut v: Vec<Session> = sessions.into_values().collect();
    v.sort_by(|a, b| a.jid.cmp(&b.jid));
    Ok(v)
}

#[tauri::command]
fn list_templates() -> Result<Vec<String>, String> {
    let cfg = load_config();
    let out = ssh_run(&cfg.host, "ls -1 \"${XDG_CONFIG_HOME:-$HOME/.config}/turing_interactive/templates\"/*.json 2>/dev/null | xargs -n1 -I{} basename {} .json")?;
    Ok(out.lines().filter(|l| !l.is_empty()).map(str::to_string).collect())
}

#[derive(Deserialize)]
pub struct SubmitArgs {
    pub template: String,
}

#[tauri::command]
fn submit_session(args: SubmitArgs, app: AppHandle) -> Result<String, String> {
    let cfg = load_config();
    if args.template.contains('\'') || args.template.contains('"') || args.template.contains(';') {
        return Err("template name contains illegal characters".into());
    }
    // sbatch_run.py writes "Submitted batch job <id>" to stdout
    let remote = format!(
        "cd '{repo}' && python sbatch_run.py --config '{tmpl}'",
        repo = cfg.repo.replace('\'', "'\\''"),
        tmpl = args.template.replace('\'', "'\\''"),
    );
    let out = ssh_run(&cfg.host, &remote)?;
    let _ = app.emit("log", format!("submit: {}", out.trim()));

    for line in out.lines() {
        if let Some(rest) = line.strip_prefix("Submitted batch job ") {
            return Ok(rest.trim().to_string());
        }
    }
    Err(format!("could not find job id in sbatch output:\n{out}"))
}

#[tauri::command]
fn cancel_session(jid: String, app: AppHandle) -> Result<(), String> {
    let cfg = load_config();
    if !jid.chars().all(|c| c.is_ascii_digit()) {
        return Err("job id must be numeric".into());
    }
    let remote = format!("scancel '{}'", jid);
    let _ = ssh_run(&cfg.host, &remote)?;
    let _ = app.emit("log", format!("scancel {jid} ok"));
    Ok(())
}

// ---------------------------------------------------------------------------
// Port forwards
// ---------------------------------------------------------------------------

#[derive(Serialize, Clone)]
pub struct ForwardInfo {
    pub id: String,
    pub label: String,
    pub host: String,        // ssh target (an alias from ~/.ssh/config, or user@host)
    pub local_port: u16,
    pub remote_host: String, // usually "localhost" — the bind on the remote side
    pub remote_port: u16,
    pub extra_args: Vec<String>, // e.g. ["-p","41234","-i","/path/to/key"]
    pub status: String,      // "starting" | "up" | "down" | "error"
}

#[derive(Deserialize)]
pub struct AddForwardArgs {
    pub label: Option<String>,
    pub host: String,
    pub local_port: u16,
    pub remote_host: Option<String>,
    pub remote_port: u16,
    pub extra_args: Option<Vec<String>>,
}

#[tauri::command]
fn add_forward(args: AddForwardArgs, app: AppHandle, state: State<'_, AppState>) -> Result<String, String> {
    if args.host.is_empty() { return Err("ssh target host is required".into()); }
    let remote_host = args.remote_host.clone().unwrap_or_else(|| "localhost".to_string());
    let extra_args = args.extra_args.unwrap_or_default();
    let id = format!("fwd-{}", state.next_id.fetch_add(1, Ordering::SeqCst));
    let label = args.label.unwrap_or_else(|| format!(
        "{}:{} → {}:{}",
        "localhost", args.local_port, remote_host, args.remote_port,
    ));

    // Pre-flight: refuse if local port is already bound (clear error vs. silent ssh failure)
    if std::net::TcpListener::bind(("127.0.0.1", args.local_port)).is_err() {
        return Err(format!("local port {} is already in use", args.local_port));
    }

    let l_spec = format!("{}:{}:{}", args.local_port, remote_host, args.remote_port);
    let mut cmd = Command::new("ssh");
    cmd.arg("-N")
        .arg("-o").arg("ExitOnForwardFailure=yes")
        .arg("-o").arg("ServerAliveInterval=30")
        .arg("-o").arg("ServerAliveCountMax=3")
        .arg("-o").arg("ConnectTimeout=15")
        .arg("-L").arg(&l_spec);
    for a in &extra_args { cmd.arg(a); }
    cmd.arg(&args.host);
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = cmd.spawn().map_err(|e| format!("could not spawn ssh: {e}"))?;

    // Stream stderr → log
    if let Some(stderr) = child.stderr.take() {
        let app2 = app.clone();
        let label2 = label.clone();
        thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines().flatten() {
                let _ = app2.emit("log", format!("[fwd {label2}] {line}"));
            }
        });
    }
    // Stream stdout (rare, ssh -N usually quiet) → log
    if let Some(stdout) = child.stdout.take() {
        let app2 = app.clone();
        let label2 = label.clone();
        thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines().flatten() {
                let _ = app2.emit("log", format!("[fwd {label2}] {line}"));
            }
        });
    }

    // Watchdog: poll the port, then poll the child for exit
    let info = ForwardInfo {
        id: id.clone(),
        label: label.clone(),
        host: args.host.clone(),
        local_port: args.local_port,
        remote_host,
        remote_port: args.remote_port,
        extra_args,
        status: "starting".into(),
    };

    {
        let mut g = state.forwards.lock().unwrap();
        g.insert(id.clone(), ForwardEntry { child, info: info.clone() });
    }

    // Spawn watcher: when child exits, remove from state and emit event
    let app3 = app.clone();
    let id_for_watch = id.clone();
    thread::spawn(move || {
        // Poll for child exit
        loop {
            thread::sleep(Duration::from_millis(400));
            let result = {
                let st: tauri::State<'_, AppState> = app3.state();
                let mut g = st.forwards.lock().unwrap();
                match g.get_mut(&id_for_watch) {
                    None => break,
                    Some(e) => e.child.try_wait(),
                }
            };
            match result {
                Ok(Some(_status)) => {
                    let st: tauri::State<'_, AppState> = app3.state();
                    let mut g = st.forwards.lock().unwrap();
                    if let Some(mut e) = g.remove(&id_for_watch) {
                        let _ = e.child.wait();
                        let _ = app3.emit("forward-ended", e.info.id.clone());
                    }
                    let _ = app3.emit("forwards-changed", ());
                    break;
                }
                Ok(None) => continue,
                Err(_) => break,
            }
        }
    });

    // Readiness poll: try to dial 127.0.0.1:local_port. As soon as we connect
    // (ssh has set up the listener), promote status to "up".
    let app4 = app.clone();
    let id_for_ready = id.clone();
    let local_port = args.local_port;
    thread::spawn(move || {
        for _ in 0..40 {  // 20s
            if std::net::TcpStream::connect_timeout(
                &format!("127.0.0.1:{local_port}").parse().unwrap(),
                Duration::from_millis(300),
            ).is_ok() {
                let st: tauri::State<'_, AppState> = app4.state();
                let mut g = st.forwards.lock().unwrap();
                if let Some(e) = g.get_mut(&id_for_ready) { e.info.status = "up".into(); }
                let _ = app4.emit("forwards-changed", ());
                return;
            }
            thread::sleep(Duration::from_millis(500));
        }
    });

    let _ = app.emit("forwards-changed", ());
    Ok(id)
}

#[tauri::command]
fn remove_forward(id: String, app: AppHandle, state: State<'_, AppState>) -> Result<(), String> {
    let mut g = state.forwards.lock().unwrap();
    if let Some(mut e) = g.remove(&id) {
        let _ = e.child.kill();
        let _ = e.child.wait();
    }
    drop(g);
    let _ = app.emit("forwards-changed", ());
    Ok(())
}

#[tauri::command]
fn list_forwards(state: State<'_, AppState>) -> Vec<ForwardInfo> {
    let g = state.forwards.lock().unwrap();
    let mut v: Vec<ForwardInfo> = g.values().map(|e| e.info.clone()).collect();
    v.sort_by(|a, b| a.id.cmp(&b.id));
    v
}

// ---------------------------------------------------------------------------
// Open URL in default browser
// ---------------------------------------------------------------------------

#[tauri::command]
fn open_url(url: String) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    let cmd_name = "xdg-open";
    #[cfg(target_os = "macos")]
    let cmd_name = "open";
    #[cfg(target_os = "windows")]
    let cmd_name = "explorer";

    Command::new(cmd_name)
        .arg(&url)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok(())
}

// ---------------------------------------------------------------------------
// SSH client command preview — for "Copy SSH" buttons in the UI we want to
// show the user the same `ssh -L …` command the launcher would run, so they
// can paste it into a terminal if they prefer.
// ---------------------------------------------------------------------------

#[tauri::command]
fn forward_command_preview(args: AddForwardArgs) -> String {
    let remote_host = args.remote_host.unwrap_or_else(|| "localhost".to_string());
    let mut parts = vec![
        "ssh".to_string(),
        "-N".to_string(),
        "-L".to_string(),
        format!("{}:{}:{}", args.local_port, remote_host, args.remote_port),
    ];
    if let Some(e) = args.extra_args { parts.extend(e); }
    parts.push(args.host);
    parts.join(" ")
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            list_ssh_hosts,
            load_config,
            save_config,
            list_sessions,
            list_templates,
            submit_session,
            cancel_session,
            add_forward,
            remove_forward,
            list_forwards,
            forward_command_preview,
            open_url,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
