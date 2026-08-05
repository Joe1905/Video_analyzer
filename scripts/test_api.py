import urllib.request
import json

try:
    url = "http://127.0.0.1:4004/api/report?date=2026-08-04&raw=1"
    req = urllib.request.urlopen(url)
    data = json.loads(req.read().decode())
    videos = data.get("videos", [])
    if videos:
        v0 = videos[0]
        print("analysis_zh type:", type(v0.get("analysis_zh")), "content:", str(v0.get("analysis_zh"))[:150])
        print("analysis type:", type(v0.get("analysis")), "content:", str(v0.get("analysis"))[:150])
        print("audit_result type:", type(v0.get("audit_result")), "content:", str(v0.get("audit_result"))[:150])
except Exception as e:
    print("Error:", e)
