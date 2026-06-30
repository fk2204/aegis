#!/bin/bash
# Install the rclone-onedrive systemd unit + start it.
# Run as root after the rclone config is in place. The script accepts either:
#   * /root/.config/rclone/rclone.conf  (created by `rclone config` over SSH OR
#                                        scp'd from Windows per RUNBOOK Path B)
#   * /home/aegis/.config/rclone/rclone.conf  (already in the aegis home tree)
# and normalizes to the aegis-readable location, since the mount unit runs as
# the aegis user.
set -e

AEGIS_RCLONE_DIR=/home/aegis/.config/rclone
AEGIS_RCLONE_CONF="$AEGIS_RCLONE_DIR/rclone.conf"
ROOT_RCLONE_CONF=/root/.config/rclone/rclone.conf

mkdir -p "$AEGIS_RCLONE_DIR"
if [ ! -f "$AEGIS_RCLONE_CONF" ]; then
  if [ -f "$ROOT_RCLONE_CONF" ]; then
    cp "$ROOT_RCLONE_CONF" "$AEGIS_RCLONE_CONF"
    echo "Copied $ROOT_RCLONE_CONF -> $AEGIS_RCLONE_CONF"
  else
    echo "ERROR: no rclone config found at $ROOT_RCLONE_CONF or $AEGIS_RCLONE_CONF" >&2
    echo "Run setup_rclone_onedrive.sh first OR scp your Windows rclone.conf to the box." >&2
    exit 1
  fi
fi
chown -R aegis:aegis "$AEGIS_RCLONE_DIR"
chmod 600 "$AEGIS_RCLONE_CONF"

cat > /etc/systemd/system/rclone-onedrive.service << 'EOF'
[Unit]
Description=RClone OneDrive Mount - AEGIS
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=aegis
Environment=RCLONE_CONFIG=/home/aegis/.config/rclone/rclone.conf
ExecStart=/usr/bin/rclone mount onedrive: /mnt/onedrive \
  --vfs-cache-mode minimal \
  --vfs-cache-max-size 500M \
  --vfs-cache-max-age 1h \
  --dir-cache-time 24h \
  --allow-other \
  --log-level INFO \
  --log-file /var/log/aegis/rclone-onedrive.log
ExecStop=/bin/fusermount3 -u /mnt/onedrive
Restart=on-failure
RestartSec=30
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable rclone-onedrive
systemctl restart rclone-onedrive
sleep 5
ls /mnt/onedrive/ && echo "Mounted OK" || echo "Failed - check: journalctl -u rclone-onedrive"
