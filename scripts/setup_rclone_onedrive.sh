#!/bin/bash
# One-time rclone install on the prod Hetzner box.
# Run as root:
#   ssh -i ~/.ssh/aegis_ci_deploy root@5.161.51.105 \
#     "bash /opt/aegis/scripts/setup_rclone_onedrive.sh"
set -e
echo "=== Installing rclone ==="
curl https://rclone.org/install.sh | sudo bash
echo "=== Installing FUSE3 ==="
apt-get install -y fuse3
mkdir -p /mnt/onedrive && chown aegis:aegis /mnt/onedrive
mkdir -p /var/log/aegis && chown aegis:aegis /var/log/aegis
echo ""
echo "Now run: rclone config"
echo "n -> new remote -> name 'onedrive' -> Microsoft OneDrive"
echo "Leave client_id/secret blank. No advanced config. Auto config = y"
echo "Open the URL on Windows, log in with Microsoft account."
echo "Select OneDrive Personal (option 1). Accept defaults. q to quit."
echo "Then: bash /opt/aegis/scripts/setup_rclone_mount.sh"
