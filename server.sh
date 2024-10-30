ssh-keygen -f "/home/nchen3/.ssh/known_hosts" -R "[gpu-4-21]:2345"
ssh nchen3@gpu-4-21 -p 2345 -oStrictHostKeyChecking=no
