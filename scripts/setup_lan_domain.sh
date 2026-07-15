#!/usr/bin/env bash
set -euo pipefail

lan_domain="${LAN_DOMAIN:-聊天.local}"
lan_ip="${LAN_IP:-192.168.1.254}"
lan_cidr="${LAN_CIDR:-192.168.0.0/23}"
http_port="${LAN_HTTP_PORT:-80}"
upstream_port="${LAN_UPSTREAM_PORT:-4003}"

if [ "${EUID}" -ne 0 ]; then
  exec sudo --preserve-env=LAN_DOMAIN,LAN_IP,LAN_CIDR,LAN_HTTP_PORT,LAN_UPSTREAM_PORT "$0" "$@"
fi

if [[ "${lan_domain}" != *.local || "${lan_domain}" == *"/"* || "${lan_domain}" == *" "* ]]; then
  echo "LAN_DOMAIN must be a valid .local name: ${lan_domain}" >&2
  exit 2
fi
if [[ ! "${lan_ip}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "LAN_IP must be an IPv4 address: ${lan_ip}" >&2
  exit 2
fi
if [[ ! "${http_port}" =~ ^[0-9]+$ || ! "${upstream_port}" =~ ^[0-9]+$ ]]; then
  echo "LAN_HTTP_PORT and LAN_UPSTREAM_PORT must be numeric." >&2
  exit 2
fi

export DEBIAN_FRONTEND=noninteractive
apt-get install -y avahi-daemon avahi-utils

systemctl stop video-analyzer-mdns-test.service 2>/dev/null || true
systemctl reset-failed video-analyzer-mdns-test.service 2>/dev/null || true
systemctl disable --now video-analyzer-mdns-legacy.service 2>/dev/null || true
rm -f /etc/systemd/system/video-analyzer-mdns-legacy.service

cat >/etc/systemd/system/video-analyzer-mdns.service <<EOF
[Unit]
Description=Publish ${lan_domain} on the local network
After=network-online.target avahi-daemon.service
Wants=network-online.target
Requires=avahi-daemon.service

[Service]
Type=simple
ExecStart=/usr/bin/avahi-publish-address -a -R ${lan_domain} ${lan_ip}
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/video-analyzer-lan-proxy.socket <<EOF
[Unit]
Description=Video Analyzer LAN HTTP socket

[Socket]
ListenStream=${lan_ip}:${http_port}
NoDelay=true

[Install]
WantedBy=sockets.target
EOF

cat >/etc/systemd/system/video-analyzer-lan-proxy.service <<EOF
[Unit]
Description=Proxy Video Analyzer LAN HTTP to port ${upstream_port}
Requires=video-analyzer-lan-proxy.socket
After=video-analyzer-lan-proxy.socket

[Service]
ExecStart=/lib/systemd/systemd-socket-proxyd 127.0.0.1:${upstream_port}
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
EOF

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  ufw allow from "${lan_cidr}" to "${lan_ip}" port "${http_port}" proto tcp comment 'Video Analyzer LAN HTTP'
  ufw allow from "${lan_cidr}" to any port 5353 proto udp comment 'Video Analyzer mDNS'
fi

systemctl daemon-reload
systemctl enable --now avahi-daemon.service
systemctl enable video-analyzer-mdns.service
systemctl restart video-analyzer-mdns.service
systemctl enable video-analyzer-lan-proxy.socket
systemctl stop video-analyzer-lan-proxy.service 2>/dev/null || true
systemctl restart video-analyzer-lan-proxy.socket

systemctl is-active --quiet avahi-daemon.service
systemctl is-active --quiet video-analyzer-mdns.service
systemctl is-active --quiet video-analyzer-lan-proxy.socket
curl --fail --silent --show-error --output /dev/null "http://${lan_ip}:${http_port}/lan-chat"

echo "LAN URL ready: http://${lan_domain}/lan-chat"
