import urllib.request
import json

try:
    url = "http://127.0.0.1:4004/api/report?date=2026-08-04&raw=1"
    req = urllib.request.urlopen(url)
    data = json.loads(req.read().decode())
    print("Report date:", data.get("report_date"))
    videos = data.get("videos", [])
    print("Videos count:", len(videos))
    if videos:
        v0 = videos[0]
        print("v0 keys:", list(v0.keys()))
        print("v0 analysis_zh:", bool(v0.get("analysis_zh")))
        print("v0 analysis:", bool(v0.get("analysis")))
        print("v0 audit_result:", bool(v0.get("audit_result")))
        print("v0 title:", v0.get("title"))
        print("v0 local_filename:", v0.get("local_filename"))
except Exception as e:
    print("Error:", e)
