#!/usr/bin/env python3
"""
Submit a SLURM job that runs a user-owned sshd on the compute node.

Differences from the v1 srun-based flow:
  * sbatch (not srun) — job survives terminal disconnects.
  * Port is picked on the compute node, not gambled here.
  * authorized_keys is per-job (turing_client_key.pub only); we never touch
    ~/.ssh/authorized_keys.
  * server_<jobid>.sh is written only after the compute-node sshd passes a
    loopback self-test, and removed on job exit — so file presence is a
    reliable readiness/liveness signal.
  * sshd runs under -D -E <log>, so failures are diagnosable.
  * All runtime state lives under XDG_STATE_HOME, not the repo.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

PWD = os.path.abspath(os.path.dirname(__file__))
HOME = os.environ["HOME"]


def xdg_state_root() -> str:
    """Single state root for everything runtime-y this tool produces.
    XDG-correct enough; one path to nuke when things go sideways."""
    root = os.environ.get("XDG_STATE_HOME") or f"{HOME}/.local/state"
    return f"{root}/turing_interactive"


def xdg_config_root() -> str:
    """User-edited templates live here (XDG config home)."""
    root = os.environ.get("XDG_CONFIG_HOME") or f"{HOME}/.config"
    return f"{root}/turing_interactive"


STATE      = xdg_state_root()
JOBS_DIR   = f"{STATE}/jobs"     # server_<jobid>.sh, sshd_<jobid>.log, slurm_<jobid>.out
SSH_DIR    = f"{STATE}/ssh"      # rendered sshd.config + authorized_keys
BATCH_DIR  = f"{STATE}/batch"    # generated sbatch wrapper scripts

CONFIG_ROOT  = xdg_config_root()
TEMPLATE_DIR = f"{CONFIG_ROOT}/templates"   # user-edited *.json job templates
EXAMPLES_DIR = f"{PWD}/examples"            # shipped starter templates


SHIM_SRC = f"{PWD}/no_nologin.c"
SHIM_SO  = f"{PWD}/no_nologin.so"


def ensure_shim_built():
    """Compile no_nologin.so if it's missing or older than no_nologin.c.

    Skipped silently if gcc isn't available — the job will run anyway and
    sshd_<jid>.log will show the /etc/nologin denial if it bites."""
    if not os.path.exists(SHIM_SRC):
        return
    if (os.path.exists(SHIM_SO) and
        os.path.getmtime(SHIM_SO) >= os.path.getmtime(SHIM_SRC)):
        return  # binary is up to date
    if not shutil.which("gcc"):
        print(f"warning: {os.path.basename(SHIM_SO)} is missing/stale and "
              "gcc isn't on PATH — nodes with /etc/nologin will reject sessions",
              file=sys.stderr)
        return
    print(f"building {os.path.basename(SHIM_SO)}…")
    r = subprocess.run(
        ["gcc", "-O2", "-shared", "-fPIC", "-o", SHIM_SO, SHIM_SRC, "-ldl"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"warning: shim build failed:\n{r.stderr}", file=sys.stderr)


def bootstrap_templates():
    """First-run only: if the user has no template directory yet, copy
    examples/*.json into it as starter content. After the directory exists
    we never touch it again — respect whatever the user has curated."""
    if os.path.isdir(TEMPLATE_DIR):
        return
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    if os.path.isdir(EXAMPLES_DIR):
        for f in os.listdir(EXAMPLES_DIR):
            if f.endswith(".json"):
                shutil.copy(os.path.join(EXAMPLES_DIR, f), TEMPLATE_DIR)


def resolve_config_path(name: str) -> str:
    """Resolve a --config argument. Accepts:
       - absolute path → used as-is
       - relative path that exists from cwd → used as-is
       - bare name like 'A100' or 'A100.json' → looked up in TEMPLATE_DIR, then EXAMPLES_DIR
    """
    if os.path.isabs(name) or os.path.exists(name):
        return name
    candidates = [name, name + ".json"] if not name.endswith(".json") else [name]
    for d in (TEMPLATE_DIR, EXAMPLES_DIR):
        for c in candidates:
            p = os.path.join(d, c)
            if os.path.exists(p):
                return p
    return name  # let the downstream open() raise with a clear FileNotFoundError

HOST_KEY   = f"{HOME}/.ssh/turing_host_key"
CLIENT_KEY = f"{HOME}/.ssh/turing_client_key"
CLIENT_PUB = f"{HOME}/.ssh/turing_client_key.pub"

RENDERED_SSHD_CONF = f"{SSH_DIR}/sshd.config"
AUTHORIZED_KEYS    = f"{SSH_DIR}/authorized_keys"

SSHD_TEMPLATE = f"{PWD}/ssh_template.config"
JOB_RUNNER    = f"{PWD}/job_runner.sh"


def run(cmd: str):
    subprocess.run(shlex.split(cmd), check=True)


def setup_keys(cleanup: bool):
    """Generate host + client keys if missing (or always if --cleanup)."""
    if cleanup or not os.path.exists(HOST_KEY):
        for p in (HOST_KEY, f"{HOST_KEY}.pub", CLIENT_KEY, CLIENT_PUB):
            if os.path.exists(p):
                os.remove(p)
        run(f'ssh-keygen -t ed25519 -f {HOST_KEY}   -q -N ""')
        run(f'ssh-keygen -t ed25519 -f {CLIENT_KEY} -q -N ""')
        os.chmod(HOST_KEY, 0o400)
        os.chmod(CLIENT_KEY, 0o400)


def render_sshd_config():
    """Render the sshd config and write the per-job authorized_keys file."""
    with open(SSHD_TEMPLATE) as f:
        c = f.read()
    c = c.replace("__AuthorizedKeysFile__", AUTHORIZED_KEYS)
    with open(RENDERED_SSHD_CONF, "w") as f:
        f.write(c)
    os.chmod(RENDERED_SSHD_CONF, 0o600)
    shutil.copyfile(CLIENT_PUB, AUTHORIZED_KEYS)
    os.chmod(AUTHORIZED_KEYS, 0o600)


def build_batch_script(config: dict) -> str:
    lines = [
        "#!/bin/bash",
        "#SBATCH -N 1",
        "#SBATCH -n 1",
        f"#SBATCH -c {config['REQCPU']}",
        f"#SBATCH --mem={config['REQMEM']}",
        f"#SBATCH --time={config['REQTIME']}",
        f"#SBATCH --partition={config['PARTITION']}",
        f"#SBATCH --output={JOBS_DIR}/slurm_%j.out",
        "#SBATCH --job-name=ssh-tunnel",
    ]
    if config.get("nodelist"):
        lines.append(f"#SBATCH --nodelist={config['nodelist']}")
    if config.get("account"):
        lines.append(f"#SBATCH --account={config['account']}")
    if config.get("REQGPU", 0):
        lines.append(f"#SBATCH --gres=gpu:{config['REQTYP']}:{config['REQGPU']}")
    lines.append("")
    # job_runner.sh args: $1=v2 source dir, $2=state root, $3=$HOME
    lines.append(f"exec bash {JOB_RUNNER} {PWD} {STATE} {HOME}")
    lines.append("")
    return "\n".join(lines)


def submit(batch_path: str) -> str:
    r = subprocess.run(["sbatch", batch_path], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"sbatch failed: {r.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    # "Submitted batch job 12345"
    return r.stdout.strip().split()[-1]


def wait_for_ready(job_id: str, timeout: int):
    server_file = f"{JOBS_DIR}/server_{job_id}.sh"
    poll = 5
    spinner = "|/-\\"
    elapsed = 0
    last_state = ""

    print(f"\nJob {job_id} submitted. Cancel with: scancel {job_id}")
    print(f"  slurm log : {JOBS_DIR}/slurm_{job_id}.out")
    print(f"  sshd log  : {JOBS_DIR}/sshd_{job_id}.log (after node assigned)\n")

    while elapsed < timeout:
        if os.path.exists(server_file) and os.path.getsize(server_file) > 0:
            return server_file
        if elapsed % 30 == 0:
            try:
                r = subprocess.run(
                    ["squeue", "-h", "-j", job_id, "-o", "%T %R"],
                    capture_output=True, text=True, timeout=5,
                )
                last_state = r.stdout.strip() or "(not in queue)"
            except Exception:
                pass
        sys.stdout.write(
            f"\r  {spinner[(elapsed // poll) % 4]}  "
            f"elapsed {elapsed}s / {timeout}s   [{last_state}]      "
        )
        sys.stdout.flush()
        time.sleep(poll)
        elapsed += poll
    return None


def main():
    ap = argparse.ArgumentParser(
        prog="sbatch_run",
        description="Spawn a user-owned sshd on a compute node via sbatch.",
    )
    ap.add_argument("--config", required=True, help="JSON config file")
    ap.add_argument("--cleanup", action="store_true",
                    help="Regenerate host and client keys before submitting")
    ap.add_argument("--wait", action="store_true",
                    help="Wait until the sshd is ready and print the connect command")
    ap.add_argument("--connect", action="store_true",
                    help="Auto-exec ssh once the node is ready (implies --wait)")
    ap.add_argument("--timeout", type=int, default=None,
                    help="Seconds to wait for readiness (default 1800)")
    ap.add_argument("--print-batch", action="store_true",
                    help="Print the generated batch script and exit without submitting")
    args = ap.parse_args()

    for d in (JOBS_DIR, SSH_DIR, BATCH_DIR):
        os.makedirs(d, exist_ok=True)

    bootstrap_templates()
    ensure_shim_built()
    setup_keys(cleanup=args.cleanup)
    render_sshd_config()

    config_path = resolve_config_path(args.config)
    with open(config_path) as f:
        config = json.load(f)

    script = build_batch_script(config)

    if args.print_batch:
        print(script)
        return

    batch_path = f"{BATCH_DIR}/job_{int(time.time())}.sh"
    with open(batch_path, "w") as f:
        f.write(script)

    job_id = submit(batch_path)
    print(f"Submitted batch job {job_id}")

    if args.wait or args.connect:
        timeout = args.timeout or config.get("WAITTIME", 1800)
        server_file = wait_for_ready(job_id, timeout)
        sys.stdout.write("\r" + " " * 78 + "\r")

        if not server_file:
            print(f"Timed out after {timeout}s. "
                  f"Check: squeue -j {job_id}  and  {JOBS_DIR}/slurm_{job_id}.out")
            sys.exit(1)

        with open(server_file) as f:
            ssh_cmd = f.read().strip()

        print("=" * 64)
        print(f"  Job {job_id} ready.")
        print(f"  {ssh_cmd}")
        print(f"  Cancel with: scancel {job_id}")
        print("=" * 64)

        if args.connect:
            print("\nConnecting...")
            os.execvp("ssh", shlex.split(ssh_cmd))


if __name__ == "__main__":
    main()
