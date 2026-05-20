# turing_interactive

A small toolkit for opening *real* interactive sessions on WPI's Turing SLURM
cluster — every job gets a user-owned `sshd` on its compute node, so you can
ssh straight to the allocation, forward ports, run a Jupyter / sglang server,
or just get a stable shell that survives login-node disconnects.

Four entry points, same underlying flow:

| Tool | Use for |
|---|---|
| `sbatch_run.py` | CLI — submit a job from the terminal |
| `smanage.py` | Terminal UI — list / connect / kill / submit sessions |
| `web_manager.py` | Browser UI — same, but in Flask |
| `desktop/` | Native cross-platform launcher (Tauri) — runs on your laptop, talks to the login node over SSH. See [desktop/README.md](desktop/README.md). |

## Why this exists

WPI's `sinteractive` opens a shell on the compute node but ties it to one
terminal: close the window, lose the session. It also doesn't let you SSH
into the compute node directly, which breaks port forwarding for things like
Jupyter or sglang. The cluster's own `sshd` on the compute nodes is locked
down and won't accept ordinary logins.

This toolkit works around that by launching a **second** `sshd` (yours,
unprivileged, on a free port) from inside the sbatch job. After authentication
all the cgroup constraints are still in effect — GPU isolation, CPU pinning,
memory limits all match what SLURM allocated.

## Architecture

```
                 LOGIN NODE                        COMPUTE NODE (inside sbatch job)
   ┌─────────────────────────────────┐      ┌─────────────────────────────────────────┐
   │ sbatch_run.py --config A100     │      │ job_runner.sh:                          │
   │   • render sshd config          │      │   • pick free TCP port                  │
   │   • write authorized_keys       │ ───▶ │   • LD_PRELOAD=no_nologin.so /usr/sbin/ │
   │   • sbatch the wrapper script   │      │     sshd -D -p $PORT -E sshd_<jid>.log  │
   │                                 │      │   • self-test via ssh user@localhost    │
   │   • poll for server_<jid>.sh    │ ◀─── │   • write server_<jid>.sh on success    │
   │   • print the connect command   │      │   • on exit/cancel: clean up everything │
   └─────────────────────────────────┘      └─────────────────────────────────────────┘
```

Key properties:

- **`server_<jid>.sh` is a liveness signal.** It appears only after a real
  loopback `ssh ... true` round-trip succeeds. It's deleted on job exit. So
  "file present" ⇔ "session is up and reachable."
- **sshd logs are surfaced.** `-E /path/to/sshd_<jid>.log` so failures aren't
  silent. (v1 had no logs; today's first big diagnostic win.)
- **Per-job `authorized_keys`.** Only `turing_client_key.pub` can get in. Your
  laptop's main `~/.ssh/authorized_keys` is not involved.
- **`/etc/nologin` is bypassed.** Some Turing compute nodes have a stale
  `/etc/nologin` from a maintenance script; OpenSSH unconditionally honors it
  for non-root users. `no_nologin.so` is a tiny `LD_PRELOAD` shim that makes
  `stat("/etc/nologin")` return `ENOENT` for sshd only. Without it, about half
  the GPU nodes silently break every session post-auth.

## Layout (XDG-correct)

```
  Repo (this directory)              Runtime
  ─────────────────────              ───────────────────────────────────────────
  sbatch_run.py                      ~/.config/turing_interactive/
  smanage.py                            templates/        user's editable JSONs
  web_manager.py                                          (bootstrapped from
  job_runner.sh                                           examples/ on first run)
  no_nologin.c  (build → .so)
  ssh_template.config                ~/.local/state/turing_interactive/
  examples/        starter JSONs        jobs/             server_<jid>.sh,
  templates/       Flask templates                        sshd_<jid>.log,
  static/          Flask static                           slurm_<jid>.out
                                        ssh/              rendered sshd.config,
                                                          per-job authorized_keys
                                        batch/            generated sbatch scripts

                                     ~/.ssh/turing_host_key      (server identity)
                                     ~/.ssh/turing_client_key    (login identity)
```

Nothing about the repo holds state. Wipe `~/.local/state/turing_interactive/`
to clear runtime data; wipe `~/.config/turing_interactive/` to reset templates.

## Install

```bash
git clone …turing_interactive
cd turing_interactive
```

That's it for the CLI — `sbatch_run.py` auto-rebuilds `no_nologin.so` from
`no_nologin.c` on first run (and whenever the source changes), as long as
`gcc` is on `PATH`.

For the web manager only: `pip install flask flask-sock paramiko` (or activate
a conda env that already has them). The in-browser terminal (xterm.js) is
loaded from a CDN, no install needed.

## Usage

### CLI

```bash
# Submit and wait until the node is ready, then print the connect command.
python sbatch_run.py --config A100 --wait

# Same, but ssh straight into the node when ready.
python sbatch_run.py --config H100 --connect

# Fire-and-forget: returns immediately with the job id.
python sbatch_run.py --config sglang_run
```

`--config` accepts a bare template name (`A100`), a bare filename
(`A100.json`), or an absolute/relative path. Bare names are looked up in
`~/.config/turing_interactive/templates/`, then in `examples/`.

Other useful flags:

| Flag | Meaning |
|---|---|
| `--wait` | Block until the node publishes `server_<jid>.sh` |
| `--connect` | `--wait` + auto-`os.execvp("ssh", …)` when ready |
| `--cleanup` | Regenerate host & client SSH keys before submitting |
| `--print-batch` | Show the generated sbatch script and exit |
| `--timeout N` | Override the wait timeout (default 1800s) |

### TUI

```bash
python smanage.py             # interactive curses UI
python smanage.py list        # non-interactive list
python smanage.py connect <#> # connect by row number or job id
python smanage.py kill <#>    # cancel
```

Inside the TUI: type to filter, `/new` to allocate, `/kill` to cancel,
`Enter` to connect, `q` to quit.

### Web

```bash
python web_manager.py --port 8001
# then from your laptop:
ssh -L 8001:localhost:8001 <login-node>
# and open http://localhost:8001
```

### Config schema

A template is plain JSON:

```json
{
    "PARTITION": "short",
    "REQCPU":    32,
    "REQMEM":    65536,
    "REQTIME":   1440,
    "REQGPU":    1,
    "REQTYP":    "rtx_pro_6000_b",
    "nodelist":  "",
    "account":   ""
}
```

| Field | Meaning |
|---|---|
| `PARTITION` | `short` / `quick` / `long` / `academic` |
| `REQCPU` | Cores per task |
| `REQMEM` | Memory in MB |
| `REQTIME` | Wall-clock limit, minutes |
| `REQGPU` | Number of GPUs (0 = CPU-only) |
| `REQTYP` | GPU type — only required if `REQGPU > 0` |
| `nodelist` | Pin to a specific node (optional) |
| `account` | SLURM account (optional) |

See `examples/` for ready-to-use starting points across the GPU pool.

## Troubleshooting

Most failures are now diagnosable from the published logs:

| Symptom | Where to look |
|---|---|
| `--wait` times out | `~/.local/state/turing_interactive/jobs/slurm_<jid>.out` |
| Job started but no `server_<jid>.sh` | `…/sshd_<jid>.log` — the actual sshd error |
| `ssh` says "Connection refused" after ready | Job was cancelled; rerun |
| `ssh` says "Bad configuration option" | Check `~/.ssh/config` for broken Host entries |
| GPU not visible from inside the session | The cgroup *should* expose only allocated GPUs; check `nvidia-smi` and `/proc/self/status` |

## Layout & v1 backup

The original `interactive_run.py`/srun design and its supporting files are
preserved under `_v1_backup/` (git-ignored). It's safe to delete that
directory once you're confident the new flow covers your workflows.
