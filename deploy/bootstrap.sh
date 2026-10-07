#!/usr/bin/env bash
# First-time hardening for the Brightly VPS (Ubuntu 24.04, BinaryLane).
# Run once as root:  bash bootstrap.sh
# Safe to re-run. Holds no client data and no secrets.
set -euo pipefail

ADMIN_USER="${ADMIN_USER:-brightly}"

[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }
[ -s /root/.ssh/authorized_keys ] || { echo "No SSH key in /root/.ssh/authorized_keys - stopping so you can't be locked out."; exit 1; }

step() { echo; echo "==> $*"; }
export DEBIAN_FRONTEND=noninteractive

step "Updating the system"
apt-get update -q
apt-get -y -q upgrade
apt-get -y -q install ca-certificates curl gnupg ufw fail2ban unattended-upgrades \
  apt-listchanges rclone age jq

step "Automatic security updates (reboots at 03:30 if a kernel update needs it)"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
cat > /etc/apt/apt.conf.d/52brightly-reboot <<'CONF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "03:30";
CONF

step "Admin user '$ADMIN_USER' (key login + sudo)"
if ! id "$ADMIN_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$ADMIN_USER"
fi
usermod -aG sudo "$ADMIN_USER"
install -d -m 700 -o "$ADMIN_USER" -g "$ADMIN_USER" "/home/$ADMIN_USER/.ssh"
install -m 600 -o "$ADMIN_USER" -g "$ADMIN_USER" /root/.ssh/authorized_keys \
  "/home/$ADMIN_USER/.ssh/authorized_keys"
# Key-only account, so sudo can't prompt for a password it doesn't have.
echo "$ADMIN_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$ADMIN_USER"
chmod 440 "/etc/sudoers.d/90-$ADMIN_USER"
visudo -cq

step "SSH: keys only, no root login"
cat > /etc/ssh/sshd_config.d/10-brightly.conf <<CONF
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
AllowUsers $ADMIN_USER
MaxAuthTries 3
X11Forwarding no
CONF
sshd -t
systemctl reload ssh 2>/dev/null || systemctl restart ssh

step "Firewall: SSH, HTTP, HTTPS only"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

step "fail2ban for SSH"
cat > /etc/fail2ban/jail.d/sshd.local <<'CONF'
[sshd]
enabled = true
maxretry = 5
bantime = 1h
CONF
systemctl enable --now fail2ban
systemctl restart fail2ban

step "Docker"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get -y -q install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
usermod -aG docker "$ADMIN_USER"
# Docker writes its own iptables rules and bypasses ufw, so containers must only
# publish ports on 127.0.0.1 (Caddy is the one exception, on 80/443).
cat > /etc/docker/daemon.json <<'CONF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "5" },
  "live-restore": true
}
CONF
systemctl restart docker

step "App folder"
install -d -m 750 -o "$ADMIN_USER" -g "$ADMIN_USER" /opt/brightly

echo
echo "Done. Now, from your PC, open a NEW PowerShell window and check you can log in:"
echo "    ssh $ADMIN_USER@$(hostname -I | awk '{print $1}')"
echo "Keep this window open until that works. Root login is now disabled."
