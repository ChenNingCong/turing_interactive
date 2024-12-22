import subprocess
import shlex
import argparse
import json
import os

PWD=os.path.abspath(os.path.split(__file__)[0])
print(PWD)

parser = argparse.ArgumentParser(
                    prog='sinteractive',
                    description='Run interactive job on slurm cluster easily')
parser.add_argument('--config', type=str, help="JSON configuration file for running job")
parser.add_argument('--gpu', type=int, help="Number of gpu, default to 4") 
parser.add_argument('--generate', default=False, action='store_true', help="Generate command for running")
parser.add_argument('--nodelist', default=None, help="Run on a special nodelist")

args = parser.parse_args()
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

print("Clean up the old key")
run_cmd_string(f"rm {PWD}/data/jupyter_key {PWD}/data/jupyter_key.pub -f")
print("Create ssh server key to connect to compute node.")
print("Regenerate server key without password")
run_cmd_string(f"""ssh-keygen -t rsa -b 4096 -f {PWD}/data/jupyter_key -q -N "" """)
print("Restrict permissions on key")
run_cmd_string(f"chmod 400 {PWD}/data/jupyter_key")
run_cmd_string(f"chmod 400 {PWD}/data/jupyter_key.pub")
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

# if REQGPU != 0:
#     REQTYP=config["REQTYP"]
#     cmd += f"--constraint={REQTYP}"
cmd += f"  bash {PWD}/sshd_script_new.sh {PWD}/data"
print(cmd)
if not args.generate:
    run_cmd_string(cmd, is_async=True)