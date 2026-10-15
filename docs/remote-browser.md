# 独立远程浏览器

入口：`/remote-browser`。复用站点的 noVNC HTTP/WebSocket 转发及网关认证，
底层端口仅监听本机，不额外开放远程桌面端口。

浏览器使用独立容器、显示通道和 `data/remote_browser/home` 持久化目录。
这是所有站点用户共享的浏览器，不要在其中登录不愿共享的个人账号。
它不占用账号 IP 池的浏览器槽位。下载文件保存在该浏览器用户目录下。

启动时从 `/etc/mihomo/config.yaml` 只提取 `apps-HY2` Hysteria2 节点，
在容器中启动独立 Mihomo 实例；不包含订阅、策略组或直连兜底。
原服务器 Mihomo 配置不变。节点更新后需重启 remote-browser 服务重新读取。

在对应服务器 checkout 中启动（先 4003 验证，再 4002）：

```bash
docker-compose -p short-video-analyzer-dev -f docker-compose.yml -f docker-compose.remote-browser.yml build remote-browser
docker-compose -p short-video-analyzer-dev -f docker-compose.yml -f docker-compose.remote-browser.yml up -d --no-deps remote-browser
docker-compose -p short-video-analyzer-dev restart web
# 验证后，在 4002 checkout 执行：
docker-compose -p short-video-analyzer -f docker-compose.yml -f docker-compose.remote-browser.yml build remote-browser
docker-compose -p short-video-analyzer -f docker-compose.yml -f docker-compose.remote-browser.yml up -d --no-deps remote-browser
docker-compose -p short-video-analyzer restart web
```

复用已有 analyzer 镜像（需含 PyYAML、Chrome、Xvfb、xdpyinfo、x11vnc、websockify），
挂载服务器已有的静态 Mihomo 二进制。附加镜像仅安装 xclip，提供 X11 文字/PNG 剪贴板。
WEB_PORT 必须与对应站点一致。内部端口分别为 WEB_PORT + 12000（noVNC）、
+13000（VNC）、+14000（专用代理）、+17000（剪贴板），显示编号等于 WEB_PORT。

剪贴板：在远程画面 Ctrl+V 可粘贴本机文字/图片；也可打开顶部“剪贴板”面板。
远程 Ctrl+C/Ctrl+X 后自动取回，文字可写回本机；通过远程右键菜单复制后，
可点击“取回远程剪贴板”。HTTPS 下可直接复制 PNG 到本机；HTTP 下请在图片预览上
右键“复制图片”（浏览器限制），不降低客户端安全设置。单次最多 16 MB。
剪贴板服务只监听本机，以浏览器用户运行；不落盘、不记录内容日志，
API 要求同源自定义请求头，不向容器转发站点认证信息。

检查：`python scripts/test_remote_browser.py`、`python scripts/test_observation_proxy.py`
均在对应环境的 Compose 容器内运行；另验证页面、noVNC 连接和浏览器实际出口。
停止或回滚只操作对应环境的 remote-browser 服务，保留用户目录。
