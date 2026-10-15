"""Dedicated persistent browser, reached through the site's observation proxy."""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time

import yaml


def proxy_config(source, port):
    node = next((p for p in source.get("proxies", []) if p.get("name") == "apps-HY2"), None)
    if not node or node.get("type") != "hysteria2" or node.get("dialer-proxy"):
        raise ValueError("需要独立的 apps-HY2 Hysteria2 节点")
    # No subscription, selector, direct route, or fallback in this instance.
    return {"mixed-port": port, "bind-address": "127.0.0.1", "allow-lan": False,
            "mode": "rule", "log-level": "warning", "ipv6": False,
            "proxies": [node], "rules": ["MATCH,apps-HY2"]}


def wait_port(port, processes):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if any(p.poll() is not None for p in processes):
            raise RuntimeError("远程浏览器子进程启动失败")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"等待本地端口 {port} 超时")


def main():
    site_port = int(os.environ.get("WEB_PORT", "4002"))
    web_port, vnc_port, proxy_port = (site_port + n for n in (12000, 13000, 14000))
    root = Path("/workspace/data/remote_browser")
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o711)
    home = root / "home"
    home.mkdir(exist_ok=True)
    os.chown(home, 10001, 10001)
    home.chmod(0o700)
    runtime = root / "proxy"
    runtime.mkdir(exist_ok=True)
    runtime.chmod(0o700)
    source = yaml.safe_load(Path("/run/mihomo-source.yaml").read_text())
    config = runtime / "config.json"
    config.touch(mode=0o600, exist_ok=True)
    config.chmod(0o600)
    config.write_text(json.dumps(proxy_config(source, proxy_port)))
    browser = shutil.which("google-chrome-stable")
    if not browser:
        raise RuntimeError("镜像中缺少 google-chrome-stable")
    processes = []
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    def launch(args, **kwargs):
        p = subprocess.Popen(args, **kwargs)
        processes.append(p)
        return p

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    display = f":{site_port}"
    env = dict(os.environ, DISPLAY=display, HOME=str(home), LANG="C.UTF-8")
    try:
        launch(["/usr/local/bin/mihomo", "-d", str(runtime), "-f", str(config)])
        wait_port(proxy_port, processes)
        launch(["Xvfb", display, "-screen", "0", "1440x900x24", "-nolisten", "tcp"])
        for _ in range(50):
            if subprocess.run(["xdpyinfo", "-display", display], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0:
                break
            if stopping or any(p.poll() is not None for p in processes):
                raise RuntimeError("独立显示通道启动失败")
            time.sleep(0.2)
        else:
            raise RuntimeError("等待独立显示通道超时")
        launch([browser, f"--user-data-dir={home / 'profile'}",
                f"--proxy-server=http://127.0.0.1:{proxy_port}",
                "--proxy-bypass-list=<-loopback>", "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--no-first-run", "--no-default-browser-check",
                "--disable-dev-shm-usage", "--disable-background-networking",
                "--disable-session-crashed-bubble", "--restore-last-session",
                "--lang=zh-CN", "--window-position=0,0", "--window-size=1440,900",
                "https://www.google.com/"], env=env, user=10001, group=10001,
               extra_groups=[])
        launch(["x11vnc", "-display", display, "-rfbport", str(vnc_port),
                "-localhost", "-forever", "-shared", "-nopw", "-quiet"])
        wait_port(vnc_port, processes)
        launch(["websockify", "--web", "/usr/share/novnc",
                f"127.0.0.1:{web_port}", f"127.0.0.1:{vnc_port}"])
        wait_port(web_port, processes)
        print(f"Remote browser ready: site={site_port}, node=apps-HY2", flush=True)
        while not stopping:
            if any(p.poll() is not None for p in processes):
                raise RuntimeError("远程浏览器子进程退出，等待容器重启")
            time.sleep(1)
    finally:
        for p in reversed(processes):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()


if __name__ == "__main__":
    main()
