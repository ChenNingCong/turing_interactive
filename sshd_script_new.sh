#!/bin/bash
# the compute node uses a differernt file system
# we must pass our home directory here
FILE_DIR=$1
PORT=$2
UHOME=$3
echo "Get directory for configuration file"
echo $FILE_DIR
echo "Terminating program on $PORT port"
lsof -t -i:$PORT | xargs -r kill 
echo "Running sshd in the background"
# we use a custom sshd configuration because we need to turn off pam protection

SERVER_FILE=$FILE_DIR/data/server/server_$SLURM_JOB_ID.sh
echo $SERVER_FILE
rm $SERVER_FILE -f
touch $SERVER_FILE

CMD="ssh $USER@$(hostname) -p $PORT -oStrictHostKeyChecking=no"

echo "Run these commands to connect the network:"
echo ""
echo "    $CMD -L 6006:localhost:6006 -L 8008:localhost:8008"
echo "(The server file is also located in $SERVER_FILE)"

echo "$CMD" > $SERVER_FILE

/usr/sbin/sshd -p $PORT -h "$UHOME/.ssh/turing_host_key" -f "$FILE_DIR/data/ssh_config/ssh.config" 
sleep infinity