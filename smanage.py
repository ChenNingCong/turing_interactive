#!/usr/bin/env python3
"""
smanage.py — Interactive session manager with a persistent bottom input bar.

Ported to v2: paths point at the XDG state root, submissions go through
v2/sbatch_run.py (which uses sbatch + compute-node port picking + LD_PRELOAD
shim where /etc/nologin is present).

Input bar (always visible at bottom):
  Type text      Filter sessions by any field
  /              Switch to command mode — autocomplete appears above bar
  /new           Allocate a new session  →  template picker  →  config form
  /kill          Kill highlighted session
  /refresh       Reload session list
  ↑ / ↓         Navigate sessions (or autocomplete list when / active)
  Enter          Connect to session  (or run selected command)
  Backspace      Delete last char
  Esc            Clear input / cancel
  q              Quit (only when input is empty)
"""

import curses
import subprocess
import argparse
import json
import os
import sys
import glob
import re

PWD  = os.path.abspath(os.path.dirname(__file__))
HOME = os.environ["HOME"]

# Share path definitions with sbatch_run.py — single source of truth.
sys.path.insert(0, PWD)
import sbatch_run  # noqa: E402
SERVER_DIR   = sbatch_run.JOBS_DIR
TEMPLATE_DIR = sbatch_run.TEMPLATE_DIR
SBATCH_RUN   = os.path.join(PWD, "sbatch_run.py")

COMMANDS = [
    ("/new",     "Allocate a new GPU/CPU session"),
    ("/kill",    "Kill highlighted session"),
    ("/refresh", "Reload session list"),
    ("/help",    "Show keyboard shortcuts"),
]

FIELD_ORDER = ["PARTITION", "REQCPU", "REQMEM", "REQTIME", "REQGPU", "REQTYP", "nodelist"]
FIELD_META  = {
    "PARTITION": ("Partition",   "short / academic / long / quick"),
    "REQCPU":    ("CPUs",        "number of cores"),
    "REQMEM":    ("Memory MB",   "16384 = 16 GB"),
    "REQTIME":   ("Time min",    "1440 = 24 h"),
    "REQGPU":    ("GPUs",        "0 = CPU-only"),
    "REQTYP":    ("GPU type",    "V100 / A100 / H100 / rtx_pro_6000_b  (ignored if GPUs=0)"),
    "nodelist":  ("Node",        "specific node, or leave blank"),
}

# ── Data ──────────────────────────────────────────────────────────────────────

def get_running_jobs():
    try:
        r = subprocess.run(
            ["squeue", "--me", "--format=%i %j %T %N %l %P", "--noheader"],
            capture_output=True, text=True)
        jobs = {}
        for line in r.stdout.strip().splitlines():
            p = line.split()
            if len(p) >= 5:
                jid, name, state, node, tlim = p[:5]
                jobs[jid] = dict(name=name, state=state, node=node,
                                 timelimit=tlim, partition=p[5] if len(p)>5 else "?")
        return jobs
    except FileNotFoundError:
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
    for path in sorted(glob.glob(os.path.join(TEMPLATE_DIR, "*.json"))):
        try:
            cfg = json.load(open(path))
            out.append((os.path.basename(path), path, cfg))
        except Exception:
            pass
    return out

def matches(row, q):
    if not q:
        return True
    q = q.lower()
    return any(q in str(v).lower() for v in row.values())

# ── SSH / job ─────────────────────────────────────────────────────────────────

def ssh_connect(ssh_cmd, job_id):
    # v2 server files already contain `-i <client_key>`; do NOT add a second one.
    _alert(f"Connecting to job {job_id}")
    os.execvp("ssh", ssh_cmd.split())

def kill_job(jid):
    subprocess.run(["scancel", jid])

def submit_job(cfg):
    """Write cfg to a tempfile and submit via v2/sbatch_run.py.

    Returns a status string for the TUI to display.
    """
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(cfg, f)
        tmp = f.name
    subprocess.Popen(
        [sys.executable, SBATCH_RUN, "--config", tmp],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return "Job submitted. Press /refresh in a few seconds."

def _alert(msg):
    sys.stdout.write("\a"); sys.stdout.flush()
    try:
        subprocess.run(["notify-send", "sinteractive", msg, "--urgency=critical"],
                       capture_output=True, timeout=2)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

# ── Drawing primitives ────────────────────────────────────────────────────────

def S(stdscr, y, x, text, attr=0):
    """Safe addstr — clips to width, skips bottom-right corner."""
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    text = text[:w - x]
    if y == h-1 and x + len(text) >= w:
        text = text[:w - x - 1]
    try:
        stdscr.addstr(y, x, text, attr) if attr else stdscr.addstr(y, x, text)
    except curses.error:
        pass

def init_colors():
    curses.init_pair(1, curses.COLOR_BLACK,   curses.COLOR_CYAN)    # selected row
    curses.init_pair(2, curses.COLOR_GREEN,   curses.COLOR_BLACK)   # READY
    curses.init_pair(3, curses.COLOR_YELLOW,  curses.COLOR_BLACK)   # PENDING / warn
    curses.init_pair(4, curses.COLOR_CYAN,    curses.COLOR_BLACK)   # header / bar
    curses.init_pair(5, curses.COLOR_BLACK,   curses.COLOR_WHITE)   # autocomplete sel
    curses.init_pair(6, curses.COLOR_MAGENTA, curses.COLOR_BLACK)   # command text
    curses.init_pair(7, curses.COLOR_BLACK,   curses.COLOR_GREEN)   # submit button
    curses.init_pair(8, curses.COLOR_WHITE,   curses.COLOR_BLACK)   # dim hint

# ── Screen sections ───────────────────────────────────────────────────────────

def draw_titlebar(stdscr, title):
    h, w = stdscr.getmaxyx()
    S(stdscr, 0, 0, title.ljust(w), curses.color_pair(4) | curses.A_BOLD)

def draw_helpbar(stdscr, text):
    h, w = stdscr.getmaxyx()
    S(stdscr, h-1, 0, text.ljust(w), curses.color_pair(4))

def draw_inputbar(stdscr, buf, placeholder=""):
    h, w = stdscr.getmaxyx()
    y = h - 2
    prompt = " > "
    S(stdscr, y, 0, prompt, curses.A_BOLD)
    display = buf if buf else placeholder
    attr = 0 if buf else curses.A_DIM
    S(stdscr, y, len(prompt), display + ("█" if buf else ""), attr)
    S(stdscr, y, 0, " " * min(w-1, len(prompt) + len(display) + 2))

def draw_session_list(stdscr, rows, filtered, sel, status_msg, top=1, reserve_bottom=3):
    h, w = stdscr.getmaxyx()

    hdr = f" {'#':<4} {'JOB ID':<12} {'STATUS':<10} {'NODE':<16} {'PART':<12} {'TIME':<10} SSH / INFO"
    S(stdscr, top, 0, hdr, curses.A_BOLD | curses.A_UNDERLINE)

    list_start = top + 1
    list_h     = h - list_start - reserve_bottom

    scroll = max(0, sel - list_h + 1) if sel >= list_h else 0

    for i, row in enumerate(filtered[scroll : scroll + list_h]):
        y      = list_start + i
        ri     = scroll + i
        is_sel = (ri == sel)
        status = row.get("status", "?")
        scol   = curses.color_pair(2) if status == "READY" else curses.color_pair(3)
        info   = row["ssh_cmd"] or f"({status} — waiting for sshd)"
        pre    = f" {ri+1:<4} {row['jid']:<12} "
        stat   = f"{status:<10} "
        suf    = f"{row['node']:<16} {row['partition']:<12} {row['timelimit']:<10} {info}"
        if is_sel:
            S(stdscr, y, 0, (pre + stat + suf).ljust(w), curses.color_pair(1) | curses.A_BOLD)
        else:
            S(stdscr, y, 0, pre)
            S(stdscr, y, len(pre), stat, scol)
            S(stdscr, y, len(pre)+len(stat), suf)

    if not filtered:
        msg = "  No sessions.  Type /new to allocate one." if not rows else "  No matches."
        S(stdscr, list_start, 0, msg, curses.A_DIM)

    if status_msg:
        S(stdscr, h-3, 0, f" {status_msg}", curses.color_pair(3) | curses.A_BOLD)

def draw_autocomplete(stdscr, items, sel):
    h, w = stdscr.getmaxyx()
    if not items:
        return
    box_h = min(len(items), 6)
    box_y_end   = h - 2
    box_y_start = box_y_end - box_h
    box_w       = min(w - 4, 70)
    box_x       = 2

    S(stdscr, box_y_start - 1, box_x, "─" * box_w, curses.color_pair(4))

    for i, (cmd, desc) in enumerate(items):
        y = box_y_start + i
        is_sel = (i == sel)
        line = f"  {cmd:<16} {desc}"[:box_w].ljust(box_w)
        if is_sel:
            S(stdscr, y, box_x, line, curses.color_pair(5) | curses.A_BOLD)
        else:
            S(stdscr, y, box_x, "  ")
            S(stdscr, y, box_x+2, f"{cmd:<16}", curses.color_pair(6) | curses.A_BOLD)
            S(stdscr, y, box_x+18, f" {desc}")

def draw_template_picker(stdscr, templates, sel, buf):
    h, w = stdscr.getmaxyx()
    hdr = f"  {'TEMPLATE':<28} {'GPU':<8} {'CPU':<6} {'MEM MB':<9} {'TIME m':<8} PARTITION"
    S(stdscr, 1, 0, hdr, curses.A_BOLD | curses.A_UNDERLINE)

    filt = [t for t in templates if buf.replace("/new","").strip().lower() in t[0].lower()]
    list_h = h - 2 - 3
    scroll = max(0, sel - list_h + 1) if sel >= list_h else 0

    for i, (label, path, cfg) in enumerate(filt[scroll:scroll+list_h]):
        y = 2 + i
        ri = scroll + i
        is_sel = (ri == sel)
        gpu  = cfg.get("REQGPU", 0)
        typ  = cfg.get("REQTYP", "")
        gpu_s = f"{gpu}×{typ}" if gpu and typ else str(gpu)
        line = f"  {label:<28} {gpu_s:<8}{cfg.get('REQCPU','?'):<6}{cfg.get('REQMEM','?'):<9}{cfg.get('REQTIME','?'):<8}{cfg.get('PARTITION','?')}"
        if is_sel:
            S(stdscr, y, 0, line.ljust(w), curses.color_pair(1) | curses.A_BOLD)
        else:
            S(stdscr, y, 0, line)

    if not filt:
        S(stdscr, 2, 0, "  No templates found.", curses.A_DIM)

    return filt

def draw_config_editor(stdscr, field_vals, field_sel, err_msg):
    h, w = stdscr.getmaxyx()
    S(stdscr, 1, 2,
      "Edit fields  ·  ↑↓ or Tab to move  ·  s to submit  ·  Esc to cancel",
      curses.A_DIM)

    col_label = 4
    col_val   = 22
    col_hint  = col_val + 24

    for i, key in enumerate(FIELD_ORDER):
        y = 3 + i * 2
        if y >= h - 4:
            break
        label, hint = FIELD_META[key]
        val = field_vals.get(key, "")
        is_sel = (i == field_sel)

        label_attr = curses.A_BOLD if is_sel else 0
        S(stdscr, y, col_label, f"{label:<16}:", label_attr)

        val_display = val + "█"
        if is_sel:
            S(stdscr, y, col_val, val_display, curses.color_pair(1) | curses.A_BOLD)
        else:
            S(stdscr, y, col_val, val or "─",
              curses.color_pair(2) if val else curses.A_DIM)

        S(stdscr, y, col_hint, f"  {hint}", curses.A_DIM)

    btn_y = 3 + len(FIELD_ORDER) * 2
    if btn_y < h - 3:
        S(stdscr, btn_y, col_label, "[ s  Submit job ]",
          curses.color_pair(7) | curses.A_BOLD)

    if err_msg:
        h2, _ = stdscr.getmaxyx()
        S(stdscr, h2 - 3, 0, f" {err_msg}", curses.color_pair(3) | curses.A_BOLD)

# ── Main TUI ──────────────────────────────────────────────────────────────────

HELP_SESSIONS  = " ↑↓ navigate  ·  type to filter  ·  /commands  ·  Enter connect  ·  q quit"
HELP_TEMPLATE  = " ↑↓ navigate  ·  type to filter  ·  Enter select  ·  Esc cancel"
HELP_CONFIG    = " ↑↓ / Tab move  ·  type to edit  ·  s submit  ·  Esc cancel"

def tui(stdscr):
    curses.curs_set(0)
    curses.use_default_colors()
    stdscr.timeout(10000)
    init_colors()

    rows         = load_sessions()
    session_sel  = 0
    status_msg   = ""
    buf          = ""           # input bar buffer
    screen       = "sessions"   # sessions | template_pick | config_edit

    templates    = []
    tpl_sel      = 0
    filt_tpls    = []

    field_vals   = {}
    field_sel    = 0
    cfg_err      = ""

    cmd_sel      = 0
    pending_kill = None

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        in_cmd   = buf.startswith("/") and screen == "sessions"
        cmd_filt = [(c, d) for c, d in COMMANDS
                    if buf.lower() in c or (len(buf)>1 and buf[1:].lower() in d.lower())] \
                   if in_cmd else []
        cmd_sel  = min(cmd_sel, max(0, len(cmd_filt) - 1))

        filter_q = "" if in_cmd else buf
        filtered = [r for r in rows if matches(r, filter_q)] \
                   if screen == "sessions" else []
        session_sel = min(session_sel, max(0, len(filtered) - 1))

        if screen == "sessions":
            draw_titlebar(stdscr, " smanage — SLURM interactive sessions ")
            autocomplete_lines = len(cmd_filt) + 2 if cmd_filt else 0
            draw_session_list(stdscr, rows, filtered, session_sel,
                              status_msg if not pending_kill else
                              f"Kill job {pending_kill}?  y = confirm  /  any key = cancel",
                              top=1, reserve_bottom=3 + autocomplete_lines)
            if cmd_filt:
                draw_autocomplete(stdscr, cmd_filt, cmd_sel)
            draw_inputbar(stdscr, buf, placeholder="filter sessions  or  /command")
            draw_helpbar(stdscr, HELP_SESSIONS)

        elif screen == "template_pick":
            draw_titlebar(stdscr, " /new — Pick a template ")
            filt_tpls = draw_template_picker(stdscr, templates, tpl_sel, buf)
            draw_inputbar(stdscr, buf, placeholder="/new  (type to filter)")
            draw_helpbar(stdscr, HELP_TEMPLATE)

        elif screen == "config_edit":
            draw_titlebar(stdscr, " /new — Configure allocation ")
            draw_config_editor(stdscr, field_vals, field_sel, cfg_err)
            cur_key = FIELD_ORDER[field_sel]
            draw_inputbar(stdscr, field_vals.get(cur_key, ""),
                          placeholder=FIELD_META[cur_key][0] + "…")
            draw_helpbar(stdscr, HELP_CONFIG)

        stdscr.refresh()

        ch = stdscr.getch()
        status_msg = ""
        cfg_err    = ""

        if ch == -1:
            rows = load_sessions(); continue

        if pending_kill is not None:
            if ch in (ord('y'), ord('Y')):
                kill_job(pending_kill)
                rows = load_sessions()
                status_msg = f"Job {pending_kill} cancelled."
            else:
                status_msg = "Kill cancelled."
            pending_kill = None
            buf = ""
            continue

        if screen == "config_edit":
            cur_key = FIELD_ORDER[field_sel]
            if ch == 27:
                screen = "sessions"; buf = ""
            elif ch == ord('s'):
                cfg = _build_cfg(field_vals)
                err = _validate_cfg(cfg)
                if err:
                    cfg_err = f"Error: {err}"
                else:
                    status_msg = submit_job(cfg)
                    rows = load_sessions()
                    screen = "sessions"; buf = ""
            elif ch in (9, curses.KEY_DOWN):
                field_sel = (field_sel + 1) % len(FIELD_ORDER)
            elif ch == curses.KEY_UP:
                field_sel = max(0, field_sel - 1)
            elif ch in (curses.KEY_ENTER, 10, 13):
                field_sel = (field_sel + 1) % len(FIELD_ORDER)
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                field_vals[cur_key] = field_vals.get(cur_key, "")[:-1]
            elif 32 <= ch <= 126:
                field_vals[cur_key] = field_vals.get(cur_key, "") + chr(ch)
            continue

        if screen == "template_pick":
            if ch == 27:
                screen = "sessions"; buf = ""
            elif ch in (curses.KEY_ENTER, 10, 13):
                if filt_tpls:
                    _, _, chosen_cfg = filt_tpls[min(tpl_sel, len(filt_tpls)-1)]
                    field_vals = {k: str(chosen_cfg.get(k, "")) for k in FIELD_ORDER}
                    field_sel = 0; screen = "config_edit"; buf = ""
            elif ch == curses.KEY_UP:
                tpl_sel = max(0, tpl_sel - 1)
            elif ch == curses.KEY_DOWN:
                tpl_sel = min(max(0, len(filt_tpls) - 1), tpl_sel + 1)
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                buf = buf[:-1]; tpl_sel = 0
            elif 32 <= ch <= 126:
                buf += chr(ch); tpl_sel = 0
            continue

        if ch == 27:
            buf = ""; cmd_sel = 0; continue

        if ch in (ord('q'), ord('Q')) and not buf:
            break

        if ch == curses.KEY_UP:
            if in_cmd and cmd_filt:
                cmd_sel = max(0, cmd_sel - 1)
            else:
                session_sel = max(0, session_sel - 1)

        elif ch == curses.KEY_DOWN:
            if in_cmd and cmd_filt:
                cmd_sel = min(len(cmd_filt) - 1, cmd_sel + 1)
            else:
                session_sel = min(len(filtered) - 1, session_sel + 1)

        elif ch == curses.KEY_PPAGE:
            session_sel = max(0, session_sel - 10)
        elif ch == curses.KEY_NPAGE:
            session_sel = min(len(filtered) - 1, session_sel + 10)

        elif ch in (curses.KEY_ENTER, 10, 13):
            if in_cmd and cmd_filt:
                chosen = cmd_filt[cmd_sel][0]
                if chosen == "/new":
                    templates = load_templates()
                    tpl_sel = 0; screen = "template_pick"
                    buf = "/new "
                elif chosen == "/kill":
                    if filtered:
                        pending_kill = filtered[session_sel]["jid"]
                    buf = ""
                elif chosen == "/refresh":
                    rows = load_sessions()
                    status_msg = "Refreshed."; buf = ""
                elif chosen == "/help":
                    status_msg = "↑↓ navigate · Enter connect · /new allocate · /kill cancel · q quit"
                    buf = ""
                cmd_sel = 0
            elif not in_cmd and filtered:
                row = filtered[session_sel]
                if row["ssh_cmd"]:
                    curses.endwin()
                    ssh_connect(row["ssh_cmd"], row["jid"])
                    return
                else:
                    status_msg = f"Job {row['jid']} not ready yet."

        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            buf = buf[:-1]; cmd_sel = 0

        elif 32 <= ch <= 126:
            buf += chr(ch); cmd_sel = 0

# ── Config helpers ────────────────────────────────────────────────────────────

def _build_cfg(fv):
    cfg = {}
    for k in FIELD_ORDER:
        v = fv.get(k, "").strip()
        if not v:
            continue
        if k in ("REQCPU", "REQMEM", "REQTIME", "REQGPU"):
            try: cfg[k] = int(v)
            except ValueError: cfg[k] = v
        else:
            cfg[k] = v
    return cfg

def _validate_cfg(cfg):
    for r in ("PARTITION", "REQCPU", "REQMEM", "REQTIME", "REQGPU"):
        if r not in cfg:
            return f"{r} is required"
    if cfg.get("REQGPU", 0) and "REQTYP" not in cfg:
        return "GPU type required when GPUs > 0"
    return None

# ── Non-TUI CLI ───────────────────────────────────────────────────────────────

def cmd_list():
    rows = load_sessions()
    if not rows:
        print("No active or pending sessions."); return
    print(f"\n {'#':<4} {'JOB ID':<12} {'STATUS':<10} {'NODE':<16} {'PARTITION':<12} {'TIME':<10} SSH COMMAND")
    print("-" * 110)
    for i, row in enumerate(rows):
        s = row["status"]
        c = "\033[92m" if s=="READY" else "\033[93m"
        print(f" {i+1:<4} {row['jid']:<12} {c}{s:<10}\033[0m {row['node']:<16} "
              f"{row['partition']:<12} {row['timelimit']:<10} {row['ssh_cmd'] or f'({s})'}")
    print()

def cmd_connect(target):
    rows = load_sessions()
    row = _resolve(rows, target)
    if row is None: print(f"Session '{target}' not found."); return
    if not row["ssh_cmd"]: print(f"Job {row['jid']} not ready yet."); return
    ssh_connect(row["ssh_cmd"], row["jid"])

def cmd_kill(target):
    rows = load_sessions()
    row = _resolve(rows, target)
    if row is None: print(f"Session '{target}' not found."); return
    if input(f"Cancel job {row['jid']}? [y/N] ").strip().lower() == "y":
        kill_job(row["jid"]); print(f"Job {row['jid']} cancelled.")

def _resolve(rows, target):
    for row in rows:
        if row["jid"] == target: return row
    try:
        idx = int(target) - 1
        if 0 <= idx < len(rows): return rows[idx]
    except ValueError: pass
    return None

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(prog="smanage",
        description="Manage sinteractive sessions")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("list")
    c = sub.add_parser("connect"); c.add_argument("id")
    k = sub.add_parser("kill");    k.add_argument("id")
    args = parser.parse_args()
    if   args.cmd == "list":    cmd_list()
    elif args.cmd == "connect": cmd_connect(args.id)
    elif args.cmd == "kill":    cmd_kill(args.id)
    else: curses.wrapper(tui)

if __name__ == "__main__":
    main()
