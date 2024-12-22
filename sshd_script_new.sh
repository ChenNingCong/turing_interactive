#!/bin/bash
# the compute node uses a differernt file system
# we must pass our home directory here
FILE_DIR=$1
echo "Get directory for configuration file"
echo $FILE_DIR
echo "Terminating program on 2345 port"
lsof -t -i:2345 | xargs -r kill 
echo "Running sshd in the background"
# we use a custom sshd configuration because we need to turn off pam protection

SERVER_FILE=$FILE_DIR/server.sh
echo $SERVER_FILE
rm $SERVER_FILE -f
touch $SERVER_FILE

CMD1="ssh-keygen -f \"/home/$USER/.ssh/known_hosts\" -R \"[$(hostname)]:2345\""
CMD2="ssh $USER@$(hostname) -p 2345 -oStrictHostKeyChecking=no"

echo "Run these commands to connect the network:"
echo ""
echo "    $CMD1"
# echo "    $CMD2"
echo "    $CMD2 -L 6006:localhost:6006 -L 8008:localhost:8008"
# echo "    $CMD2 -t -L 6006:localhost:6006 -L 8008:localhost:8008 'tmux attach -t ssh_tmux || tmux new-session -s ssh_tmux' "
echo "(The server file is also located in $SERVER_FILE)"

echo "$CMD1" > $SERVER_FILE
echo "$CMD2" >> $SERVER_FILE

/usr/sbin/sshd -p 2345 -h "$FILE_DIR/data/jupyter_key" -f "$FILE_DIR/jupyter_ssh.config" 
sleep infinity