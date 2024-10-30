#!/bin/bash
trap exit
# the compute node uses a differernt file system
# we must pass our home directory here
FILE_DIR=$1
echo "Get directory for configuration file"
echo $FILE_DIR
echo "Terminating program on 2345 port"
lsof -t -i:2345 | xargs -r kill 
echo "Running sshd in the background"
# we use a custom sshd configuration because we need to turn off pam protection
/usr/sbin/sshd -p 2345 -h $FILE_DIR/jupyter_key -f $FILE_DIR/jupyter_ssh.config 
echo "Run these commands to connect the network:"
echo ""
echo "    ssh-keygen -f \"/home/$USER/.ssh/known_hosts\" -R \"[$(hostname)]:2345\""
echo "    ssh $USER@$(hostname) -p 2345 -oStrictHostKeyChecking=no"
echo ""
bash
exit 0