import subprocess
import shlex
import argparse
import json
import os
from random import randrange

PWD=os.path.abspath(os.path.split(__file__)[0])
print(PWD)
HOME=os.getenv("HOME")
print(HOME)

parser = argparse.ArgumentParser(
                    prog='sinteractive',
                    description='Run interactive job on slurm cluster easily')
parser.add_argument('--config', type=str, help="JSON configuration file for running job")
parser.add_argument('--gpu', type=int, help="Number of gpu, default to 4") 
parser.add_argument('--generate', default=False, action='store_true', help="Generate command for running")
parser.add_argument('--nodelist', default=None, help="Run on a special nodelist")
parser.add_argument('--port', default=None, help="Open a port, must specified to avoid conflict")
parser.add_argument('--cleanup', action="store_true", help="Regenerate all the keys")
args = parser.parse_args()
PORT = args.port
if PORT is None:
    # hope that we are lucky!
    print("WARNING : randomly open a port!")
    PORT = randrange(3000, 8000)

if args.config is None:
    if args.gpu == 0:
        args.config = f"{PWD}/cpu_default.json"
    else:
        args.config = f"{PWD}/gpu_default.json"
    
"""
Execute a script in python
"""
def run_cmd_string(cmd : str, is_async=False):
    args = shlex.split(cmd)
    if is_async:
        subprocess.Popen(args)
    else:
        subprocess.run(args)
import os
os.makedirs(f"{PWD}/data/", exist_ok=True)
os.makedirs(f"{PWD}/data/server", exist_ok=True)
os.makedirs(f"{PWD}/data/ssh_config", exist_ok=True)
if not os.path.exists(f"{HOME}/.ssh/turing_host_key") or args.cleanup:
    print("Create new key")
    print("Generate server key without password")
    run_cmd_string(f"""ssh-keygen -t rsa -b 4096 -f {HOME}/.ssh/turing_host_key -q -N "" """)
    run_cmd_string(f"chmod 400 {HOME}/.ssh/turing_host_key")
    run_cmd_string(f"chmod 400 {HOME}/.ssh/turing_host_key.pub")
    print("Generate client key without password for login")
    run_cmd_string(f"""ssh-keygen -t rsa -b 4096 -f {HOME}/.ssh/turing_client_key -q -N "" """)
    run_cmd_string(f"chmod 400 {HOME}/.ssh/turing_client_key")
    run_cmd_string(f"chmod 400 {HOME}/.ssh/turing_client_key.pub")

print("Generate new ssh server config")
# only allow the client to login!
setting = {'__AuthorizedKeysFile__' : f'{HOME}/.ssh/authorized_keys'}
with open(f"{PWD}/ssh_template.config", 'r') as f:
    c = f.read()
    for i in setting:
        c = c.replace(i, setting[i])
    with open(f"{PWD}/data/ssh_config/ssh.config", 'w') as f:
        f.write(c)

print("Loading config")
with open(args.config) as f:
    config = json.load(f)

REQCPU=config["REQCPU"]
REQMEM=config["REQMEM"]
REQTIME=config["REQTIME"]
PARTITION=config["PARTITION"]
REQGPU=config["REQGPU"]
if args.gpu is not None:
    REQGPU=args.gpu

cmd = f"""
srun -N 1 \
-c {REQCPU}  \
-n 1 \
--mem={REQMEM} \
--time={REQTIME} \
--partition={PARTITION} \
--gres=gpu:{REQGPU} \
"""
if args.nodelist is not None:
    cmd += f"--nodelist={args.nodelist }"

if REQGPU != 0:
    REQTYP=config["REQTYP"]
    cmd += f"--constraint={REQTYP}"
cmd += f"  bash {PWD}/sshd_script_new.sh {PWD} {PORT} {HOME}"
print(cmd)
if not args.generate:
    run_cmd_string(cmd, is_async=True)