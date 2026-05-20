#!/usr/bin/env python3
"""
web_manager.py — Browser-based session manager for the Turing cluster.

Ported to v2: reads/writes XDG state, delegates job submission to
v2/sbatch_run.py (no duplicated batch-script generation), and fixes the
sync_ssh_config bug that left orphan blocks in ~/.ssh/config.

Usage:
    python web_manager.py [--port 5000] [--host 0.0.0.0]

Then open http://localhost:5000 in your browser.
For remote access tunnel the port:
    ssh -L 5000:localhost:5000 <login-node>
"""

import glob
import json
import os
import re
import subprocess
import sys

from flask import Flask, jsonify, render_template, request
from flask_sock import Sock

PWD  = os.path.abspath(os.path.dirname(__file__))
HOME = os.environ["HOME"]

# Share path definitions with sbatch_run.py — single source of truth.
sys.path.insert(0, PWD)
import sbatch_run  # noqa: E402
SERVER_DIR = sbatch_run.JOBS_DIR
TMPL_DIR   = sbatch_run.TEMPLATE_DIR
SBATCH_RUN = os.path.join(PWD, "sbatch_run.py")

app = Flask(__name__, template_folder="templates", static_folder="static")
sock = Sock(app)

# ── Data helpers ──────────────────────────────────────────────────────────────

def get_running_jobs():
    try:
        r = subprocess.run(
            ["squeue", "--me", "--format=%i|%j|%T|%N|%l|%P|%Q|%S", "--noheader"],
            capture_output=True, text=True, timeout=5,
        )
        jobs = {}
        for line in r.stdout.strip().splitlines():
            p = line.split("|")
            if len(p) >= 5:
                jid, name, state, node, tlim = p[:5]
                jobs[jid] = dict(
                    name=name, state=state, node=node.strip() or None,
                    timelimit=tlim, partition=p[5] if len(p) > 5 else "?",
                    priority=p[6] if len(p) > 6 else None,
                    start_time=p[7].strip() if len(p) > 7 else None,
                )
        return jobs
    except Exception:
        return {}


def get_session_files():
    out = {}
    for path in glob.glob(os.path.join(SERVER_DIR, "server_*.sh")):
        m = re.search(r"server_(\d+)\.sh$", path)
        if m:
            content = open(path).read().strip()
            if content:
                out[m.group(1)] = content
    return out


def load_sessions():
    running = get_running_jobs()
    files   = get_session_files()
    rows = []
    for jid in sorted((k for k in files if k in running), key=int):
        rows.append(dict(jid=jid, ssh_cmd=files[jid], status="READY", **running[jid]))
    for jid in sorted((k for k in running if k not in files), key=int):
        rows.append(dict(jid=jid, ssh_cmd=None, status=running[jid]["state"], **running[jid]))
    return rows


def load_templates():
    sbatch_run.bootstrap_templates()
    out = []
    for path in glob.glob(os.path.join(TMPL_DIR, "*.json")):
        try:
            cfg = json.load(open(path))
            out.append({"name": os.path.basename(path), "path": path,
                        "config": cfg, "mtime": os.path.getmtime(path)})
        except Exception:
            pass
    out.sort(key=lambda t: t["mtime"], reverse=True)
    return out


SSH_CONFIG_PATH = os.path.join(HOME, ".ssh", "config")
SSH_BLOCK_START = "# >>> turing-session-manager (do not edit) >>>"
SSH_BLOCK_END   = "# <<< turing-session-manager <<<"


def _parse_v2_ssh_cmd(ssh_cmd):
    """Parse v2 ssh command:
       ssh -i <key> -p PORT -o ... -o ... user@host
    Returns (user, hostname, port) or None.
    """
    user_host = re.search(r"\s(\S+)@(\S+)\s*$", ssh_cmd)
    port = re.search(r"-p\s+(\d+)", ssh_cmd)
    if not user_host or not port:
        return None
    return user_host.group(1), user_host.group(2), port.group(1)


def sync_ssh_config(sessions):
    """Rewrite the turing-managed block in ~/.ssh/config to match active READY sessions.

    Bug fix vs. v1: strip ALL existing turing-session-manager blocks (not just the
    first one's start..first-end), which is what left orphan blocks behind. We
    locate the FIRST opener and the LAST closer, and replace that whole span.
    """
    os.makedirs(os.path.dirname(SSH_CONFIG_PATH), exist_ok=True)

    existing = ""
    if os.path.exists(SSH_CONFIG_PATH):
        with open(SSH_CONFIG_PATH) as f:
            existing = f.read()

    before, after = existing, ""
    if SSH_BLOCK_START in existing:
        start = existing.index(SSH_BLOCK_START)
        # use rfind on END to swallow any accumulated/orphaned closers
        end = existing.rfind(SSH_BLOCK_END)
        before = existing[:start]
        after  = existing[end + len(SSH_BLOCK_END):] if end != -1 and end > start else ""

    entries = []
    for s in sessions:
        if not s.get("ssh_cmd"):
            continue
        parsed = _parse_v2_ssh_cmd(s["ssh_cmd"])
        if not parsed:
            continue
        user, hostname, port = parsed
        node  = s.get("node", s["jid"])
        alias = f"turing-{node}-{s['jid']}"
        client_key = f"{HOME}/.ssh/turing_client_key"
        entry = (
            f"Host {alias}\n"
            f"    HostName {hostname}\n"
            f"    User {user}\n"
            f"    Port {port}\n"
            f"    IdentityFile {client_key}\n"
            f"    StrictHostKeyChecking no\n"
            f"    UserKnownHostsFile /dev/null\n"
        )
        entries.append(entry)

    if entries:
        block = SSH_BLOCK_START + "\n\n" + "\n".join(entries) + SSH_BLOCK_END + "\n"
    else:
        block = ""

    new_config = before.rstrip("\n") + ("\n\n" if before.strip() else "") + block + after.lstrip("\n")
    with open(SSH_CONFIG_PATH, "w") as f:
        f.write(new_config)


def _preview_batch_script(cfg):
    """Use sbatch_run.py --print-batch to render the batch script without submitting.

    Avoids duplicating job-script generation logic in the web layer.
    """
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(cfg, f); tmp = f.name
    try:
        r = subprocess.run(
            [sys.executable, SBATCH_RUN, "--config", tmp, "--print-batch"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout
    finally:
        os.unlink(tmp)


# ── API routes ─────────────────────────────────────────────────────────────────

@app.get("/api/fairshare")
def api_fairshare():
    user = os.getenv("USER") or os.path.basename(HOME)
    try:
        rs = subprocess.run(
            ["sshare", "--parsable2", "--noheader"],
            capture_output=True, text=True, timeout=5,
        )
        rows = {}
        for line in rs.stdout.strip().splitlines():
            p = line.split("|")
            if len(p) >= 7 and p[1].strip() == user:
                acct = p[0].strip()
                rows[acct] = {
                    "account":     acct,
                    "fairshare":   p[6].strip(),
                    "effec_usage": p[5].strip(),
                    "partitions":  [],
                    "default_partition": None,
                }

        ra = subprocess.run(
            ["sacctmgr", "show", "association", f"user={user}", "--parsable2", "--noheader"],
            capture_output=True, text=True, timeout=5,
        )
        for line in ra.stdout.strip().splitlines():
            p = line.split("|")
            if len(p) >= 19:
                acct = p[1].strip()
                qos  = p[17].strip()
                defq = p[18].strip()
                if acct in rows:
                    rows[acct]["partitions"] = [q for q in qos.split(",") if q]
                    rows[acct]["default_partition"] = defq or None

        return jsonify({"ok": True, "rows": list(rows.values()), "user": user})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.get("/api/gpu_types")
def api_gpu_types():
    try:
        r = subprocess.run(
            ["sinfo", "--format=%G", "--noheader"],
            capture_output=True, text=True, timeout=5,
        )
        types = set()
        for line in r.stdout.splitlines():
            for token in line.split(","):
                token = token.strip()
                if token.startswith("gpu:") and token != "gpu:(null)":
                    parts = token.split(":")
                    if len(parts) >= 2 and parts[1]:
                        types.add(parts[1])
        return jsonify(sorted(types))
    except Exception:
        return jsonify([])


@app.get("/api/sessions")
def api_sessions():
    sessions = load_sessions()
    try:
        sync_ssh_config(sessions)
    except Exception:
        pass  # never break the sessions response over a config write failure
    return jsonify(sessions)


@app.get("/api/templates")
def api_templates():
    return jsonify(load_templates())


@app.post("/api/templates")
def api_save_template():
    body = request.json
    name = body.get("name", "").strip()
    cfg  = body.get("config", {})
    if not name:
        return jsonify({"error": "name required"}), 400
    if not name.endswith(".json"):
        name += ".json"
    if os.sep in name or "/" in name:
        return jsonify({"error": "invalid name"}), 400
    path = os.path.join(TMPL_DIR, name)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=4)
    return jsonify({"ok": True, "name": name})


@app.delete("/api/templates/<name>")
def api_delete_template(name):
    if os.sep in name or "/" in name:
        return jsonify({"error": "invalid name"}), 400
    path = os.path.join(TMPL_DIR, name)
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    os.remove(path)
    return jsonify({"ok": True})


@app.post("/api/preview")
def api_preview():
    cfg = request.json
    return jsonify({"script": _preview_batch_script(cfg)})


@app.post("/api/allocate")
def api_allocate():
    cfg = request.json
    required = ["REQCPU", "REQMEM", "REQTIME", "PARTITION", "REQGPU"]
    missing = [k for k in required if k not in cfg]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(cfg, f); tmp = f.name
    try:
        r = subprocess.run(
            [sys.executable, SBATCH_RUN, "--config", tmp],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return jsonify({"error": (r.stderr or r.stdout).strip()}), 500
        # sbatch_run.py prints "Submitted batch job <ID>"
        m = re.search(r"Submitted batch job (\d+)", r.stdout)
        if not m:
            return jsonify({"error": f"unexpected sbatch_run output: {r.stdout.strip()}"}), 500
        return jsonify({"ok": True, "job_id": m.group(1)})
    finally:
        os.unlink(tmp)


@app.post("/api/kill/<jid>")
def api_kill(jid):
    if not jid.isdigit():
        return jsonify({"error": "invalid job id"}), 400
    r = subprocess.run(["scancel", jid], capture_output=True, text=True)
    if r.returncode != 0:
        return jsonify({"error": r.stderr.strip()}), 500
    return jsonify({"ok": True})


# ── In-browser SSH terminal (xterm.js ↔ paramiko bridge) ─────────────────────

CLIENT_KEY = f"{HOME}/.ssh/turing_client_key"


def _parse_server_file(cmd: str):
    """Parse a v2 server_<jid>.sh line:
       ssh -i KEY -p PORT -o ... -o ... user@host
    Returns (user, host, port) or None."""
    m_p  = re.search(r"-p\s+(\d+)", cmd)
    m_uh = re.search(r"(\S+?)@(\S+)\s*$", cmd)
    if not (m_p and m_uh):
        return None
    return m_uh.group(1), m_uh.group(2), int(m_p.group(1))


@sock.route("/ws/ssh/<jid>")
def ws_ssh(ws, jid):
    """Browser ↔ compute-node shell bridge. Short-lived SSH per WS connection;
    the user runs tmux themselves if they want persistence."""
    import paramiko
    import threading

    if not jid.isdigit():
        ws.send(json.dumps({"type": "error", "msg": "bad job id"}))
        return
    sf = os.path.join(SERVER_DIR, f"server_{jid}.sh")
    if not os.path.exists(sf):
        ws.send(json.dumps({"type": "error", "msg": "session not ready or already gone"}))
        return
    parsed = _parse_server_file(open(sf).read().strip())
    if not parsed:
        ws.send(json.dumps({"type": "error", "msg": "cannot parse server file"}))
        return
    user, host, port = parsed

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host, port=port, username=user,
            key_filename=CLIENT_KEY,
            timeout=10, look_for_keys=False, allow_agent=False,
        )
    except Exception as e:
        ws.send(json.dumps({"type": "error", "msg": f"ssh connect failed: {e}"}))
        return

    channel = client.invoke_shell(term="xterm-256color", width=120, height=30)

    def pump_browser_to_shell():
        """Read JSON control messages from WS, write to shell."""
        try:
            while True:
                msg = ws.receive()
                if msg is None:
                    break
                try:
                    o = json.loads(msg) if isinstance(msg, str) else {}
                except (ValueError, TypeError):
                    o = {}
                t = o.get("type")
                if t == "data":
                    channel.send(o.get("payload", "").encode("utf-8"))
                elif t == "resize":
                    try:
                        channel.resize_pty(width=int(o["cols"]), height=int(o["rows"]))
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try: channel.close()
            except Exception: pass

    th = threading.Thread(target=pump_browser_to_shell, daemon=True)
    th.start()

    # Main loop: shell → WS as raw UTF-8.
    try:
        while True:
            data = channel.recv(4096)
            if not data:
                break
            try:
                ws.send(data.decode("utf-8", errors="replace"))
            except Exception:
                break
    finally:
        try: channel.close()
        except Exception: pass
        try: client.close()
        except Exception: pass


@app.get("/")
def index():
    return render_template("index.html")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Turing web session manager")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    p.add_argument("--port", default=5000, type=int, help="Port (default: 5000)")
    p.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    a = p.parse_args()
    os.makedirs(SERVER_DIR, exist_ok=True)
    print(f"Starting Turing Session Manager on http://{a.host}:{a.port}")
    print(f"  state dir : {sbatch_run.STATE}")
    print(f"  templates : {TMPL_DIR}")
    if a.host == "127.0.0.1":
        print(f"  Tip: tunnel with  ssh -L {a.port}:localhost:{a.port} <login-node>")
    app.run(host=a.host, port=a.port, debug=a.debug)
