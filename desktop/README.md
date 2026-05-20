# Turing Interactive — Native Desktop Launcher

A Tauri (Rust + HTML) desktop app that talks to the cluster login node over
plain SSH and lets you:

- list / submit / cancel SLURM sessions (the ones `sbatch_run.py` manages),
- open arbitrary `ssh -L` port forwards from the login node *or* from a
  compute node (via the per-job sshd that `sbatch_run.py` brings up),
- copy the connect command for any session straight into the clipboard.

The point is to skip the "open VS Code → terminal → activate conda → `python
web_manager.py` → forward the port → open browser" dance every morning. The
launcher itself runs on your laptop; nothing new is installed on the cluster
(the existing `sbatch_run.py` + state-dir layout is what it queries over
SSH).

## How it talks to the cluster

Every cluster interaction is one `ssh login-host -- bash -lc '<remote>'`
invocation, using whatever's already in your `~/.ssh/config` and ssh-agent —
the app never prompts for a password.

| Action       | Remote command (paraphrased)                                                                         |
|--------------|------------------------------------------------------------------------------------------------------|
| List sessions| `squeue -h -u $USER -o ...` + reading `~/.local/state/turing_interactive/jobs/server_*.sh`           |
| List templates| `ls ~/.config/turing_interactive/templates/*.json`                                                 |
| Submit       | `cd <repo> && python sbatch_run.py --config <template>`                                              |
| Cancel       | `scancel <jid>`                                                                                      |
| Port forward | local: `ssh -N -L <local>:<host>:<remote> <ssh-target> [extra args]`                                 |

For forwards from a compute node, the launcher parses the per-job `ssh -p N
-i /path/to/key user@compute-node` command published in `server_<jid>.sh` and
re-uses those `-p` / `-i` flags on the local side.

## Prerequisites

### Local (laptop)

- Rust toolchain — install via https://rustup.rs
- Tauri CLI — `cargo install tauri-cli --version "^2.0"`
- An `ssh` binary on PATH:
  - **Linux/macOS** — already there.
  - **Windows** — install "OpenSSH Client" from *Apps → Optional Features*.
- Working SSH access to your login node via key auth (try `ssh <login-host>` first; if it logs in without typing a password, the launcher will too).

### Linux build deps

The first `cargo tauri build` (or `cargo tauri dev`) needs WebKitGTK 4.1 and
GLib ≥ 2.70 system-wide. On Ubuntu 22.04+ / Debian 12+:

```bash
sudo apt install \
  libwebkit2gtk-4.1-dev \
  libgtk-3-dev \
  libayatana-appindicator3-dev \
  librsvg2-dev \
  build-essential pkg-config
```

(On older distros, e.g. Ubuntu 20.04 with GLib 2.64, this app **cannot be
built**. Build it on something newer.)

### Cluster (login node) — must already have

This app does not install anything remotely. It assumes the existing
`turing_interactive` repo is checked out and working:

- `~/turing_interactive/` (or wherever you point the launcher) contains
  `sbatch_run.py` from the parent repo.
- Conda env that has `python` on PATH (the launcher runs `bash -lc`, so your
  login shell needs to bring conda + python in).
- `~/.local/state/turing_interactive/` is where `sbatch_run.py` writes
  `jobs/server_*.sh` — the launcher reads that path directly. (Override via
  `XDG_STATE_HOME` if you've changed it.)

## Run it

```bash
cd desktop
cargo tauri dev
```

First launch opens a window with no host configured. Click **Settings**,
pick a host out of your `~/.ssh/config` dropdown, set the repo path on the
login node (default `~/turing_interactive`), Save. The sessions list will
populate.

## Build installers

The default config has `bundle.active: false` so the dev experience needs
zero icons. To make real installers:

1. Generate icons from a 1024×1024 PNG (only once):
   ```bash
   cd desktop/src-tauri
   cargo tauri icon path/to/your_logo.png
   ```
   This populates `src-tauri/icons/` with platform-specific variants.

2. Flip `"bundle"."active"` to `true` in `desktop/src-tauri/tauri.conf.json`.

3. Build:
   ```bash
   cd desktop
   cargo tauri build
   ```

   Output (per OS you're building on):
   - Linux: `src-tauri/target/release/bundle/{appimage,deb}/…`
   - Windows: `src-tauri/target/release/bundle/{msi,nsis}/…`
   - macOS: `src-tauri/target/release/bundle/{macos,dmg}/…`

Cross-compiling is not configured here. To get Windows installers, build on
Windows (or set up Tauri's cross-build action later — see the
"Future / not-yet" section in the parent README).

## Walkthroughs

### "I want a Jupyter notebook running on the compute node, opening in my browser"

1. On the **Sessions** tab, click **Submit** with a GPU template
   (e.g. `A100`). Wait for status `RUNNING` and an SSH Command to appear.
2. Inside that session (from a separate terminal, or a future native
   terminal tab), launch Jupyter listening on, say, port `8888`:
   ```bash
   jupyter notebook --no-browser --port 8888
   ```
3. Back in the launcher, click **Forward…** on that session's row. The
   modal pre-fills the `-p`/`-i` flags from the per-job sshd. Set both
   ports to `8888` and click **Open tunnel**.
4. Switch to the **Forwards** tab and click `localhost:8888` — your default
   browser opens the notebook.

### "I want the web manager UI on the login node"

(This case exists only if you also want the old Flask UI; the launcher
already covers session lifecycle on its own.)

1. Forwards tab → **+ Add forward** → SSH target = your login alias,
   local & remote port = `8001`, Open tunnel.
2. SSH into the login node separately and run
   `python web_manager.py --port 8001`.
3. Click `localhost:8001` from the Forwards tab.

## Caveats / known limits

- **SSH config `Include` directives are not followed** by the launcher's
  parser. If you use `Include ~/.ssh/config.d/*`, those hosts won't show up
  in the dropdown — type the alias in by hand in Settings or move them into
  the main config.
- **Wildcard hosts** (`Host *`) are dropped from the dropdown — they are
  not directly connectable.
- **No native terminal yet.** "Copy SSH" puts the command on your
  clipboard. A built-in xterm tab is a follow-up.
- **Templates are listed by basename** (no preview of the JSON). The full
  template JSON lives on the login node at
  `~/.config/turing_interactive/templates/`; edit it there.
- **Local port collision** — the launcher refuses to open a forward if the
  local port is already in use; switch ports rather than fighting it.
