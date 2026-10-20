"""Run on the public Ubuntu host from a GitHub checkout, as root.

Installs a restricted service and adds ONLY the media path to ruitech.hk's
existing Caddy site. Never replaces unrelated site configuration.
"""
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def install():
    if os.geteuid() != 0:
        raise SystemExit("Run as root")
    source = Path(__file__).with_name("buffer_media_server.py")
    caddy = Path("/root/website/Caddyfile")
    original = caddy.read_text()
    gateway = run("docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}", "ruitech-caddy")
    if not re.fullmatch(r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.1", gateway):
        raise SystemExit("Unexpected Docker bridge gateway; inspect before deployment")
    needle = "ruitech.hk, www.ruitech.hk {\n\treverse_proxy web:80"
    replacement = ("ruitech.hk, www.ruitech.hk {\n\thandle /buffer-media/* {\n"
                   f"\t\treverse_proxy {gateway}:8788\n\t}}\n"
                   "\thandle {\n\t\treverse_proxy web:80\n\t}")
    if "/buffer-media/*" not in original and original.count(needle) != 1:
        raise SystemExit("Caddy site differs; no configuration changed")
    if not Path("/etc/video-analyzer-media").exists():
        run("useradd", "--system", "--home-dir", "/var/lib/video-analyzer-media", "--shell", "/usr/sbin/nologin", "buffer-media")
    for name, mode in (("/etc/video-analyzer-media", 0o750), ("/var/lib/video-analyzer-media", 0o750), ("/opt/video-analyzer-media", 0o755)):
        path = Path(name)
        path.mkdir(exist_ok=True)
        path.chmod(mode)
        shutil.chown(path, "buffer-media", "buffer-media")
    token = Path("/etc/video-analyzer-media/token")
    if not token.exists():
        token.write_text(secrets.token_urlsafe(48))
    token.chmod(0o600)
    shutil.chown(token, "buffer-media", "buffer-media")
    shutil.copyfile(source, "/opt/video-analyzer-media/server.py")
    Path("/etc/systemd/system/video-analyzer-media.service").write_text(f"""[Unit]
Description=Video Analyzer media ingress
After=network-online.target docker.service
Requires=docker.service
[Service]
User=buffer-media
Group=buffer-media
Environment=BUFFER_MEDIA_BIND={gateway}
ExecStart=/usr/bin/python3 /opt/video-analyzer-media/server.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/video-analyzer-media
UMask=0077
[Install]
WantedBy=multi-user.target
""")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "video-analyzer-media")
    run("systemctl", "restart", "video-analyzer-media")
    run("systemctl", "is-active", "video-analyzer-media")
    if "/buffer-media/*" not in original:
        backup = caddy.with_name("Caddyfile.before-buffer-media")
        if backup.exists():
            raise SystemExit("Backup already exists; inspect before replacing Caddy config")
        shutil.copyfile(caddy, backup)
        # Preserve the bind mount's inode; atomic replace would leave Caddy reading the old file.
        caddy.write_text(original.replace(needle, replacement, 1))
        try:
            run("docker", "exec", "ruitech-caddy", "caddy", "validate", "--config", "/etc/caddy/Caddyfile")
            run("docker", "exec", "ruitech-caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile")
        except subprocess.CalledProcessError:
            caddy.write_text(original)
            run("docker", "exec", "ruitech-caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile")
            raise
    print("Media service installed; upload credential is stored locally and is not printed.")


if __name__ == "__main__":
    install()
