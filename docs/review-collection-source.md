# 4003 复盘关联采集数据

视频列表与采集结果按精确 TikTok 视频 ID 关联，不按标题或账号名称匹配。
默认读取当前环境的采集库；`REVIEW_COLLECTION_DB` 可指定额外只读采集库。
优先选择最近有有效相邻秒留存的完整快照，其他指标也来自同一快照。

4003 当前的数据源采用正式采集库只读绑定，正式服务及数据不被修改。
保留现有 Compose 配置，不加入 `docker-compose.dev.yml`，避免更换当前挂载目录：

```bash
cd /home/openclaw/Video_analyzer-dev
REVIEW_COLLECTION_DB_HOST=/home/openclaw/Video_analyzer/data/proxy_pool.sqlite \
docker-compose -p short-video-analyzer-dev -f docker-compose.yml -f docker-compose.review-source.yml up -d --no-deps web
```

后续重建/up 命令需保留此覆盖文件及变量；`restart web` 不改变挂载配置。
当前来源为 SQLite DELETE 日志模式。如来源改为 WAL，需要同时暴露 WAL/SHM 文件，不能只绑定主库。
页面标注关联及采集时间。新增可用留存后，重新打开复盘会更新分析，复用原有视频提取。
