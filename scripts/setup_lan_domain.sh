#!/usr/bin/env bash
set -euo pipefail

formal_domain="${FORMAL_LAN_DOMAIN:-tymy.local}"
beta_domain="${BETA_LAN_DOMAIN:-tymy-beta.local}"
lan_ip="${LAN_IP:-192.168.1.254}"
lan_cidr="${LAN_CIDR:-192.168.0.0/23}"
http_port="${LAN_HTTP_PORT:-80}"
formal_upstream_port="${FORMAL_UPSTREAM_PORT:-4002}"
beta_upstream_port="${BETA_UPSTREAM_PORT:-4003}"

if [ "${EUID}" -ne 0 ]; then
  exec sudo --preserve-env=FORMAL_LAN_DOMAIN,BETA_LAN_DOMAIN,LAN_IP,LAN_CIDR,LAN_HTTP_PORT,FORMAL_UPSTREAM_PORT,BETA_UPSTREAM_PORT "$0" "$@"
fi

for domain in "${formal_domain}" "${beta_domain}"; do
  if [[ "${domain}" != *.local || "${domain}" == *"/"* || "${domain}" == *" "* ]]; then
    echo "LAN domains must be valid .local names: ${domain}" >&2
    exit 2
  fi
done
if [ "${formal_domain}" = "${beta_domain}" ]; then
  echo "Formal and beta LAN domains must be different." >&2
  exit 2
fi
if [[ ! "${lan_ip}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "LAN_IP must be an IPv4 address: ${lan_ip}" >&2
  exit 2
fi
for port in "${http_port}" "${formal_upstream_port}" "${beta_upstream_port}"; do
  if [[ ! "${port}" =~ ^[0-9]+$ ]]; then
    echo "LAN proxy ports must be numeric: ${port}" >&2
    exit 2
  fi
done

export DEBIAN_FRONTEND=noninteractive
systemctl disable --now video-analyzer-lan-proxy.socket 2>/dev/null || true
systemctl stop video-analyzer-lan-proxy.service 2>/dev/null || true
rm -f /etc/systemd/system/video-analyzer-lan-proxy.socket
rm -f /etc/systemd/system/video-analyzer-lan-proxy.service
systemctl daemon-reload

apt-get install -y avahi-daemon avahi-utils nginx

systemctl stop video-analyzer-mdns-test.service 2>/dev/null || true
systemctl reset-failed video-analyzer-mdns-test.service 2>/dev/null || true
systemctl disable --now video-analyzer-mdns-legacy.service 2>/dev/null || true
rm -f /etc/systemd/system/video-analyzer-mdns-legacy.service

cat >/etc/systemd/system/video-analyzer-mdns.service <<EOF
[Unit]
Description=Publish ${formal_domain} on the local network
After=network-online.target avahi-daemon.service
Wants=network-online.target
Requires=avahi-daemon.service

[Service]
Type=simple
ExecStart=/usr/bin/avahi-publish-address -a -R ${formal_domain} ${lan_ip}
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/video-analyzer-mdns-beta.service <<EOF
[Unit]
Description=Publish ${beta_domain} on the local network
After=network-online.target avahi-daemon.service
Wants=network-online.target
Requires=avahi-daemon.service

[Service]
Type=simple
ExecStart=/usr/bin/avahi-publish-address -a -R ${beta_domain} ${lan_ip}
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/nginx/sites-available/video-analyzer-lan <<EOF
server {
    listen ${lan_ip}:${http_port} default_server;
    server_name ${formal_domain};
    client_max_body_size 2g;

    location / {
        proxy_pass http://127.0.0.1:${formal_upstream_port};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}

server {
    listen ${lan_ip}:${http_port};
    server_name ${beta_domain};
    client_max_body_size 2g;

    location / {
        proxy_pass http://127.0.0.1:${beta_upstream_port};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
EOF
rm -f /etc/nginx/sites-enabled/default
ln -sfn /etc/nginx/sites-available/video-analyzer-lan /etc/nginx/sites-enabled/video-analyzer-lan

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  ufw allow from "${lan_cidr}" to "${lan_ip}" port "${http_port}" proto tcp comment 'Video Analyzer LAN HTTP'
  ufw allow from "${lan_cidr}" to any port 5353 proto udp comment 'Video Analyzer mDNS'
fi

systemctl daemon-reload
systemctl enable --now avahi-daemon.service
systemctl enable video-analyzer-mdns.service
systemctl restart video-analyzer-mdns.service
systemctl enable video-analyzer-mdns-beta.service
systemctl restart video-analyzer-mdns-beta.service
nginx -t
systemctl enable nginx.service
systemctl restart nginx.service

systemctl is-active --quiet avahi-daemon.service
systemctl is-active --quiet video-analyzer-mdns.service
systemctl is-active --quiet video-analyzer-mdns-beta.service
systemctl is-active --quiet nginx.service
curl --fail --silent --show-error --header "Host: ${formal_domain}" --output /dev/null "http://${lan_ip}:${http_port}/"
curl --fail --silent --show-error --header "Host: ${beta_domain}" --output /dev/null "http://${lan_ip}:${http_port}/lan-chat"

echo "Formal LAN URL ready: http://${formal_domain}/"
echo "Beta LAN URL ready: http://${beta_domain}/lan-chat"
