#!/bin/bash
# Install the rclone-onedrive systemd unit + start it.
# Run as root after `rclone config` has authorized the 'onedrive' remote.
set -e
cat > /etc/systemd/system/rclone-onedrive.service << 'EOF'
[Unit]
Description=RClone OneDrive Mount - AEGIS
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=aegis
ExecStart=/usr/bin/rclone mount onedrive: /mnt/onedrive \
  --vfs-cache-mode full --vfs-cache-max-size 10G --vfs-cache-max-age 24h \
  --allow-other --log-level INFO --log-file /var/log/aegis/rclone-onedrive.log
ExecStop=/bin/fusermount3 -u /mnt/onedrive
Restart=on-failure
RestartSec=30
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable rclone-onedrive
systemctl start rclone-onedrive
sleep 5
ls /mnt/onedrive/ && echo "Mounted OK" || echo "Failed - check: journalctl -u rclone-onedrive"
