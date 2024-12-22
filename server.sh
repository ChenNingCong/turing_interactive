ssh-keygen -f "/home/zzhang18/.ssh/known_hosts" -R "[gpu-4-13]:2345"
ssh zzhang18@gpu-4-13 -p 2345 -oStrictHostKeyChecking=no
