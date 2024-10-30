import subprocess
import shlex
import argparse
import json
import os

PWD=os.path.split(__file__)[0]
print(PWD)

parser = argparse.ArgumentParser(
                    prog='sinteractive',
                    description='Run interactive job on slurm cluster easily')
parser.add_argument('--config', default=f"{PWD}/cpu_default.json", type=str, help="JSON configuration file for running job") 
parser.add_argument('--cpu', default=False, action='store_true', help="Use only cpu") 
parser.add_argument('--gpu', default=4, type=int, help="Number of gpu, default to 4") 
parser.add_argument('--generate', default=False, action='store_true', help="Generate command for running")

args = parser.parse_args()
if args.cpu:
    args.config = f"{PWD}/cpu_default.json"
else:
    args.config = f"{PWD}/gpu_default.json"
"""
Execute a script in python
"""
def run_cmd_string(cmd : str):
    args = shlex.split(cmd)
    subprocess.Popen(args)

print("Clean up the old key")
run_cmd_string("rm jupyter_key jupyter_key.pub -f")
print("Create ssh server key to connect to compute node.")
print("Regenerate server key without password")
run_cmd_string("""ssh-keygen -t rsa -b 4096 -f jupyter_key -q -N "" """)
print("Restrict permissions on key")
run_cmd_string(f"chmod 400 {PWD}/jupyter_key")
run_cmd_string(f"chmod 400 {PWD}/jupyter_key.pub")
print("Loading config")
with open(args.config) as f:
    config = json.load(f)

REQCPU=config["REQCPU"]
REQMEM=config["REQMEM"]
REQTIME=config["REQTIME"]
PARTITION=config["PARTITION"]
if args.cpu:
    REQGPU=0
else:
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
# if REQGPU != 0:
#     REQTYP=config["REQTYP"]
#     cmd += f"--constraint={REQTYP}"
if not args.generate:
    cmd += f"  bash {PWD}/sshd_script_new.sh {PWD}"
print(cmd)
if not args.generate:
    run_cmd_string(cmd)