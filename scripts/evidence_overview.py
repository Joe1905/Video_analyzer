"""Build a chronological evidence index without model calls or semantic inference."""
import json

from standardize_analysis import frame_evidence


def build_video_description(frame_analyses, transcript=None):
    timeline, issues = frame_evidence(frame_analyses)
    groups = []
    previous_key = None
    previous_index = -2
    for frame in timeline:
        key = (frame["visual"], json.dumps(frame["visible_text"], ensure_ascii=False),
               json.dumps(frame["uncertainties"], ensure_ascii=False), frame["time_source"])
        second = frame["timestamp_seconds"]
        if key == previous_key and frame["index"] == previous_index + 1:
            groups[-1]["last_sample_seconds"] = second
            groups[-1]["frame_ids"].append(frame["evidence_id"])
        else:
            groups.append({"first_sample_seconds": second, "last_sample_seconds": second,
                           "time_source": frame["time_source"], "frame_ids": [frame["evidence_id"]]})
        previous_key = key
        previous_index = frame["index"]
    transcript = transcript or {}
    segments = []
    for index, segment in enumerate(transcript.get("segments") or []):
        if not isinstance(segment, dict):
            continue
        start, end = segment.get("start"), segment.get("end")
        timed = (isinstance(start, (int, float)) and not isinstance(start, bool)
                 and isinstance(end, (int, float)) and not isinstance(end, bool) and 0 <= start <= end)
        segments.append({"segment_index": index, "start": start, "end": end,
                         "sampled_frame_ids": [f["evidence_id"] for f in timeline
                            if timed and f["time_source"] == "capture"
                            and start <= f["timestamp_seconds"] <= end]})
    overview = {"source": "script", "version": 1, "frame_groups": groups,
                "transcript_segment_index": segments, "evidence_issues": issues,
                "limitations": ["仅合并描述、可见文字和不确定项完全相同的相邻采样帧；不判断场景或动作。",
                                 "分组起止是采样点，不表示期间画面持续相同；ASR关联只表示时间重叠。"]}
    summary = (f"脚本证据概览：{len(timeline)}条有效帧记录，{len(groups)}组相邻记录，"
               f"{len(segments)}段ASR，{len(issues)}项帧证据问题。"
               "按下方时间索引查看原始画面描述与口播；内容机制由报告分析归纳。")
    return {"processing_source": "script", "response": json.dumps(
        {"summary": summary, "evidence_overview": overview}, ensure_ascii=False)}
