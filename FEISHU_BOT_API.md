# Video Analyzer Feishu Bot API

## 1. Base

Base URL:

```text
http://192.168.1.254:4002
```

Request header:

```http
Content-Type: application/json
```

Async job convention:

- Create job endpoints return `202 Accepted` and an `id`.
- Poll the matching `*-job?id=...` endpoint.
- Common `status` values: `queued`, `running`, `complete`, `failed`.
- On success, read `result`, `analysis`, or `extract`.
- On failure, read `error` and `log`.

## 2. Daily Report

Get today's report:

```http
GET /api/report/feishu
```

Get report by date:

```http
GET /api/report/feishu?date=2026-06-30&limit=10
```

Response:

```json
{
  "ok": true,
  "exists": true,
  "report_date": "2026-06-30",
  "status": "complete",
  "title": "2026-06-30 爆款视频日报",
  "summary": "...",
  "url": "http://192.168.1.254:4002/report?date=2026-06-30",
  "generated_at": 1782768386.8711188,
  "video_count": 10,
  "analysis_success_count": 10,
  "analysis_failed_count": 0,
  "error": "",
  "report": {},
  "report_markdown": "...",
  "videos": [],
  "feishu_text": "..."
}
```

Recommended display:

- Title: `title`
- Body: prefer `feishu_text`
- Link: `url`
- Video list: `videos`

History:

```http
GET /api/report/history
```

Trigger today's report:

```http
POST /api/report/run
```

## 3. Chat / Tool Agent

Ask:

```http
POST /api/chat/ask
```

Request:

```json
{
  "sessionId": "feishu-user-123",
  "message": "帮我分析这个 TikTok 链接：https://..."
}
```

Response:

```json
{
  "sessionId": "feishu-user-123",
  "userMessage": {
    "id": "...",
    "role": "user",
    "content": "...",
    "status": "done"
  },
  "message": {
    "id": "...",
    "role": "assistant",
    "content": "",
    "status": "pending"
  }
}
```

List sessions:

```http
GET /api/chat/sessions
```

Get session:

```http
GET /api/chat/sessions/{sessionId}
```

List tools:

```http
GET /api/chat/tools
```

## 4. Video Download

Create download job:

```http
POST /api/download
```

Request:

```json
{
  "url": "https://www.tiktok.com/@user/video/123",
  "source_tag": "api_upload"
}
```

`source_tag` is optional. If omitted, the backend uses `api_upload`.

Source tags:

```text
web_manual  Manual upload or URL submitted from the analyzer web page. Visible in /extract video list.
hot_report  URL captured by the daily report workflow. Hidden from /extract video list.
api_upload  Video or URL submitted by an external API client. Hidden from /extract video list by default.
```

Response:

```json
{
  "id": "...",
  "url": "...",
  "status": "queued",
  "filename": "",
  "error": "",
  "log": [],
  "result": null
}
```

Poll:

```http
GET /api/download-job?id=<job_id>
```

Success fields:

```json
{
  "status": "complete",
  "filename": "shortvideo_TikTok_123.mp4",
  "result": {}
}
```

## 5. Video Upload

Upload one or more local video files:

```http
POST /api/upload
Content-Type: multipart/form-data
```

Form fields:

```text
video       Required. One or more video files.
source_tag  Optional. Defaults to api_upload.
```

Recommended API upload:

```bash
curl -X POST "http://192.168.1.254:4002/api/upload" \
  -F "video=@/path/to/video.mp4" \
  -F "source_tag=api_upload"
```

Manual web upload equivalent:

```bash
curl -X POST "http://192.168.1.254:4002/api/upload" \
  -F "video=@/path/to/video.mp4" \
  -F "source_tag=web_manual"
```

Response:

```json
{
  "files": [
    {
      "filename": "video.mp4",
      "size": 1234567
    }
  ],
  "errors": []
}
```

Partial success is possible. Check both `files` and `errors`.

Visibility note:

- `web_manual` uploads are shown in the analyzer `/extract` video list.
- `api_upload` uploads are accepted and analyzable by filename, but are hidden from the analyzer `/extract` video list.
- `hot_report` is reserved for daily report captured URLs and should not be used by normal API upload clients.

After upload, call `/api/analyze` with the returned `filename`.

## 6. Video Analysis

Create analysis job:

```http
POST /api/analyze
```

Request:

```json
{
  "filename": "shortvideo_TikTok_123.mp4",
  "analysis_mode": "analyzer",
  "postprocess": false,
  "reset_output": false
}
```

Direct video mode:

```json
{
  "filename": "shortvideo_TikTok_123.mp4",
  "analysis_mode": "direct_video",
  "postprocess": false,
  "reset_output": false
}
```

Response:

```json
{
  "status": "queued",
  "filename": "shortvideo_TikTok_123.mp4"
}
```

Poll:

```http
GET /api/job?id=<job_id>
```

Get result:

```http
GET /api/result?filename=shortvideo_TikTok_123.mp4
```

Response fields:

```json
{
  "analysis": {},
  "analysis_zh": {},
  "audit_result": {},
  "audit_result_zh": {},
  "feedback_result": {},
  "feedback_result_zh": {}
}
```

Get stable workflow feedback:

```http
GET /api/video-feedback?filename=shortvideo_TikTok_123.mp4
GET /api/video-feedback?download_job_id=<download_job_id>
GET /api/video-feedback?job_id=<analysis_job_id>
GET /api/video-feedback?download_job_id=<download_job_id>&job_id=<analysis_job_id>
```

This endpoint is the recommended status interface for external systems. Use it to decide whether `/api/result` is ready instead of guessing from job completion alone.

Stable `state` values:

```text
downloading      Download job is queued or running.
uploaded         Video file exists, but extraction has not started.
queued           Extraction or analysis is queued.
extracting       Video content extraction is running.
analyzing        DeepSeek report generation is running.
analysis_ready   Extraction result exists and /api/result can be read.
metrics          Social/comment metrics are being collected; /api/result can still be read if has_analysis_text=true.
completed        Analysis/report is complete.
failed           Download, extraction, or analysis failed.
```

Response:

```json
{
  "ok": true,
  "state": "analysis_ready",
  "label": "分析结果已生成",
  "filename": "shortvideo_TikTok_123.mp4",
  "download_job_id": "...",
  "job_id": "...",
  "file_ready": true,
  "extraction_complete": true,
  "analysis_complete": false,
  "metrics_complete": false,
  "has_analysis_text": true,
  "can_read_result": true,
  "result_url": "/api/result?filename=shortvideo_TikTok_123.mp4",
  "queue_status": "analyzed",
  "progress": {},
  "failure_stage": "",
  "failure_reason": "",
  "download": {},
  "job": {},
  "updated_at": 1782768386.8711188
}
```

Integration rule:

- Only call `/api/result` when `can_read_result=true`.
- If `state=analysis_ready`, read extraction fields such as `analysis`, `analysis_zh`, `direct_analysis`, or `direct_analysis_zh`.
- If `state=completed`, report fields such as `audit_result` or `direct_audit_result` may also be available.
- If `state=failed`, display `failure_stage`, `failure_reason`, and useful `download.log` or `job.log` lines.

Get processed social fields:

```http
GET /api/social-processed?filename=shortvideo_TikTok_123.mp4
```

Use this endpoint when writing the processed `评论`、`数据`、`博主分析` columns back to a table. It returns summarized, table-ready text plus structured fields. It is different from `/api/result`, which returns the broader saved JSON payload.

Response:

```json
{
  "filename": "shortvideo_TikTok_123.mp4",
  "source_url": "https://www.tiktok.com/@user/video/123",
  "status": "complete",
  "updated_at": 1782768386.8711188,
  "summary": "...",
  "table_fields": {
    "评论": "评论洞察或评论样本...",
    "数据": "播放:1000，点赞:100，评论:20\n数据洞察...",
    "博主分析": "博主:creator，粉丝:10000，作品:50\n博主洞察..."
  },
  "comments": {
    "status": "ok",
    "count": 20,
    "sample_count": 20,
    "samples": [],
    "insight": "...",
    "error": ""
  },
  "data": {
    "status": "ok",
    "video": {},
    "metrics": {},
    "insight": "...",
    "error": ""
  },
  "creator": {
    "status": "ok",
    "profile": {},
    "metrics": {},
    "insight": "...",
    "error": ""
  },
  "recommended_actions": []
}
```

Translate:

```http
POST /api/translate
```

Request:

```json
{
  "filename": "shortvideo_TikTok_123.mp4",
  "tab": "content"
}
```

`tab` values:

```text
content
audit
feedback
```

Generate DeepSeek analysis:

```http
POST /api/postprocess
```

Request:

```json
{
  "filename": "shortvideo_TikTok_123.mp4"
}
```

## 7. TikTok Shop

Create extraction job:

```http
POST /api/shop-extract
```

Request:

```json
{
  "url": "https://www.tiktok.com/shop/...",
  "source_type": "product",
  "region": "US",
  "max_pages": 1,
  "review_pages": 1,
  "analyze": true,
  "related_videos": false,
  "prompt": ""
}
```

`source_type` values:

```text
product
details
reviews
shop
search
```

Poll:

```http
GET /api/shop-job?id=<job_id>
```

Response fields:

```json
{
  "id": "...",
  "status": "complete",
  "extract": {},
  "analysis": {},
  "error": "",
  "log": []
}
```

## 8. TikTok / Social Metrics

Create metrics job:

```http
POST /api/video-metrics
```

Request:

```json
{
  "endpoint": "video-info",
  "target": "https://www.tiktok.com/@user/video/123"
}
```

Common `endpoint` values:

```text
video-info
profile
user-posts
search-top
trending
music-popular
```

Notes:

- `video-info`, `profile`, `user-posts`, and `search-top` need `target`.
- `trending` and `music-popular` can run without `target`.

Poll:

```http
GET /api/video-metrics-job?id=<job_id>
```

Response fields:

```json
{
  "id": "...",
  "endpoint": "video-info",
  "target": "...",
  "status": "complete",
  "result": {},
  "error": "",
  "log": []
}
```

## 9. Amazon

Create Amazon scrape job:

```http
POST /api/amazon-scrape
```

Request:

```json
{
  "target": "https://www.amazon.com/dp/XXXX",
  "target_type": "url",
  "pages": 1
}
```

Common `target_type` values:

```text
url
keyword
asin
```

Poll:

```http
GET /api/amazon-job?id=<job_id>
```

Response fields:

```json
{
  "id": "...",
  "status": "complete",
  "target": "...",
  "target_type": "url",
  "url": "...",
  "pages": 1,
  "result": {},
  "error": "",
  "log": []
}
```

## 10. Suggested Feishu Bot Commands

Daily report:

```text
/日报
GET /api/report/feishu
```

Daily report by date:

```text
/日报 2026-06-30
GET /api/report/feishu?date=2026-06-30
```

Download video:

```text
/下载视频 <url>
POST /api/download
GET /api/download-job?id=<job_id>
```

Upload video:

```text
/上传视频 <file>
POST /api/upload
POST /api/analyze
GET /api/job?id=<job_id>
GET /api/result?filename=<filename>
```

Analyze video:

```text
/分析视频 <filename>
POST /api/analyze
GET /api/job?id=<job_id>
GET /api/result?filename=<filename>
```

Video metrics:

```text
/查视频数据 <url>
POST /api/video-metrics
GET /api/video-metrics-job?id=<job_id>
```

Request:

```json
{
  "endpoint": "video-info",
  "target": "<url>"
}
```

TikTok Shop:

```text
/查TikTokShop <url>
POST /api/shop-extract
GET /api/shop-job?id=<job_id>
```

Amazon:

```text
/查Amazon <url或关键词>
POST /api/amazon-scrape
GET /api/amazon-job?id=<job_id>
```

Natural-language agent:

```text
/问 <自然语言>
POST /api/chat/ask
```

## 11. Feishu Implementation Notes

Async job flow:

1. User sends a command.
2. Feishu bot calls the create-job endpoint.
3. Bot replies: task created and processing.
4. Poll every 3-10 seconds.
5. Send result when `status=complete`.
6. Send `error` and useful `log` lines when `status=failed`.

Display priority:

```text
Daily report: feishu_text > report_markdown > summary
Video analysis: analysis_zh > analysis > audit_result_zh > audit_result
TikTok Shop: analysis > extract
Amazon: result
Social metrics: result
```

Long text:

- Do not send large JSON blobs directly.
- Prefer summary fields.
- Truncate raw result or attach it as a file.
- For daily report, use `feishu_text`.

Error response:

```json
{
  "error": "error reason"
}
```

Failed job:

```json
{
  "status": "failed",
  "error": "error reason",
  "log": []
}
```

## 12. Minimum Integration Set

Phase 1:

```text
GET  /api/report/feishu
POST /api/upload
POST /api/download
GET  /api/download-job
GET  /api/video-feedback
GET  /api/social-processed
POST /api/analyze
GET  /api/job
GET  /api/result
POST /api/video-metrics
GET  /api/video-metrics-job
POST /api/chat/ask
GET  /api/chat/sessions
```

Phase 2:

```text
POST /api/shop-extract
GET  /api/shop-job
POST /api/amazon-scrape
GET  /api/amazon-job
POST /api/translate
POST /api/postprocess
```
