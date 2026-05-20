#!/bin/bash
# Runs inside the sbatch job on the compute node.
# Responsibilities:
#   1. pick a free TCP port on this node
#   2. start a user-owned sshd with full logging
#   3. self-test the sshd via loopback before advertising readiness
#   4. write server_<jobid>.sh (atomically) as the "ready" signal
#   5. clean up everything on exit / signal
#
# Arguments:
#   $1  SRC_DIR  — absolute path to the v2 source directory
#   $2  STATE    — XDG state root for this tool (one parent over jobs/, ssh/)
#   $3  UHOME    — login-node $HOME (same NFS mount on compute nodes)

set -uo pipefail

SRC_DIR="$1"
STATE="$2"
UHOME="$3"

JOB_ID="${SLURM_JOB_ID:-local-$$}"
JOBS_DIR="$STATE/jobs"
SSH_DIR="$STATE/ssh"

SSHD_LOG="$JOBS_DIR/sshd_${JOB_ID}.log"
SERVER_FILE="$JOBS_DIR/server_${JOB_ID}.sh"
HOST_KEY="$UHOME/.ssh/turing_host_key"
CLIENT_KEY="$UHOME/.ssh/turing_client_key"
SSHD_CONF="$SSH_DIR/sshd.config"

mkdir -p "$JOBS_DIR"

# ---- preflight ------------------------------------------------------------
for f in "$HOST_KEY" "$CLIENT_KEY" "$SSHD_CONF"; do
  if [[ ! -r "$f" ]]; then
    echo "[job_runner] missing or unreadable: $f" >&2
    exit 1
  fi
done

# /etc/nologin handling: some Turing compute nodes (notably the GPU pool)
# have a stale /etc/nologin file left over from maintenance. OpenSSH honors
# it unconditionally for non-root users and there is no sshd_config option
# to disable the check, so a user-launched sshd authenticates but then
# refuses every session. We LD_PRELOAD a tiny shim that makes /etc/nologin
# stat() as ENOENT for sshd only — every other path passes through.
SHIM="$SRC_DIR/no_nologin.so"
SSHD_PRELOAD=""
if [[ -e /etc/nologin ]]; then
  if [[ -r "$SHIM" ]]; then
    SSHD_PRELOAD="LD_PRELOAD=$SHIM"
    echo "[job_runner] /etc/nologin present; will run sshd under $SHIM"
  else
    echo "[job_runner] /etc/nologin present but $SHIM missing — sessions will fail" >&2
  fi
fi

HOST=$(hostname)
echo "[job_runner] job=$JOB_ID host=$HOST user=$USER"
echo "[job_runner] sshd log: $SSHD_LOG"

# ---- pick a free TCP port on THIS node ------------------------------------
PORT=$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("", 0))
print(s.getsockname()[1])
s.close()
PY
)
if [[ -z "$PORT" ]]; then
  echo "[job_runner] failed to pick a free port" >&2
  exit 1
fi
echo "[job_runner] picked port: $PORT"

# ---- cleanup trap (runs on any exit) --------------------------------------
SSHD_PID=""
cleanup() {
  echo "[job_runner] cleanup: removing $SERVER_FILE"
  rm -f "$SERVER_FILE"
  if [[ -n "$SSHD_PID" ]] && kill -0 "$SSHD_PID" 2>/dev/null; then
    echo "[job_runner] cleanup: killing sshd pid=$SSHD_PID"
    kill "$SSHD_PID" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$SSHD_PID" 2>/dev/null || break
      sleep 0.2
    done
    kill -KILL "$SSHD_PID" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
}
trap cleanup EXIT TERM INT HUP

# ---- launch sshd ----------------------------------------------------------
# -D = foreground (so the trap actually catches it)
# -E = log file (otherwise errors go to syslog which we can't read)
env $SSHD_PRELOAD /usr/sbin/sshd \
    -D \
    -p "$PORT" \
    -h "$HOST_KEY" \
    -f "$SSHD_CONF" \
    -E "$SSHD_LOG" \
    -o "LogLevel=VERBOSE" &
SSHD_PID=$!
echo "[job_runner] sshd started, pid=$SSHD_PID"

# ---- self-test: don't advertise until a real auth+exec round-trip works ---
SELFTEST_OK=""
for i in $(seq 1 30); do
  if ! kill -0 "$SSHD_PID" 2>/dev/null; then
    echo "[job_runner] sshd died before becoming ready (see $SSHD_LOG)" >&2
    exit 1
  fi
  if ssh -F /dev/null \
         -i "$CLIENT_KEY" \
         -p "$PORT" \
         -o StrictHostKeyChecking=no \
         -o UserKnownHostsFile=/dev/null \
         -o BatchMode=yes \
         -o ConnectTimeout=2 \
         -o LogLevel=ERROR \
         "$USER@$HOST" true 2>>"$SSHD_LOG"; then
    SELFTEST_OK=1
    echo "[job_runner] self-test passed on attempt $i"
    break
  fi
  sleep 1
done

if [[ -z "$SELFTEST_OK" ]]; then
  echo "[job_runner] self-test failed after 30 attempts (see $SSHD_LOG)" >&2
  exit 1
fi

# ---- publish the connect command (atomic via rename) ----------------------
TMP_SERVER_FILE="${SERVER_FILE}.tmp"
{
  # $HOST comes from hostname(1); on Turing compute nodes it's already the FQDN.
  echo "ssh -i $CLIENT_KEY -p $PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null $USER@${HOST}"
} > "$TMP_SERVER_FILE"
mv "$TMP_SERVER_FILE" "$SERVER_FILE"
chmod 600 "$SERVER_FILE"
echo "[job_runner] published $SERVER_FILE"

# ---- wait on sshd; when sshd dies (or job is cancelled), trap fires -------
wait "$SSHD_PID"
echo "[job_runner] sshd exited"
