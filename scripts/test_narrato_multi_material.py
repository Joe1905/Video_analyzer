"""Run in the trial container; --real uses the three uploaded product videos and live models."""
import asyncio
import json
import sys
import subprocess
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services.narrato_multi_material import MultiMaterialAnalysisService, resolve_selection


def check():
    from app.services.clip_video import _process_mixed_segment
    with TemporaryDirectory() as directory:
        source = str(Path(directory) / "source.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=160x120:r=25",
                        "-t", "5", "-c:v", "libx264", source], check=True)
        output = _process_mixed_segment(source,
            {"_id": 1, "timestamp": "00:00:00,000-00:00:01,000", "OST": 2},
            {1: {"duration": 2.8}}, directory,
            {"video_codec": "libx264", "audio_codec": "aac", "pixel_format": "yuv420p", "preset": "ultrafast", "quality_value": "23"}, [])
        duration = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", output]))
        assert 2.7 < duration < 3.1, duration
    service = MultiMaterialAnalysisService()
    analyzed = {"analysis_artifact": {"batches": [{"status": "success", "frame_observations": [{"observation": "发光玩具"}]}]}, "keyframe_files": ["frame.jpg"]}
    for language in (None, "Español"):
        with patch.object(service, "analyze_video", new_callable=AsyncMock, return_value=analyzed) as vision, \
                patch.object(service, "_timestamp_from_keyframe_name", return_value="00:00:00,000"), \
                patch("app.services.narrato_multi_material.subprocess.check_output", return_value=b"3"), \
                patch("app.services.narrato_multi_material.UnifiedLLMService.generate_text", new_callable=AsyncMock,
                      return_value='{"items":[{"clip_id":"v1f1","narration":"Light up your play."}]}') as llm:
            options = {} if language is None else {"script_language": language}
            result = asyncio.run(service.generate_documentary_script(
                product_mode=True, product_description="发光玩具", video_path="/tmp/a.mp4", **options))
            expected = "成片脚本语言：" + (language or "English")
            assert expected in llm.call_args.kwargs["system_prompt"]
            assert expected in vision.call_args.kwargs["custom_prompt"]
            assert "广告口播" in llm.call_args.kwargs["system_prompt"]
            assert "广告口播" not in vision.call_args.kwargs["custom_prompt"]
            assert result[0]["narration"] == "Light up your play."
    for description in ["", " \n\t", None]:
        try:
            asyncio.run(MultiMaterialAnalysisService().generate_documentary_script(
                product_mode=True, product_description=description, video_path="/missing.mp4"))
        except ValueError as exc:
            assert "商品描述" in str(exc)
        else:
            raise AssertionError("Empty product description accepted")
    paths = ["/tmp/a.mp4", "/tmp/b.mp4"]
    candidates = [{"clip_id": "b1", "video_id": 2, "timestamp": "00:00:01,000-00:00:02,000", "picture": "detail"}]
    result = resolve_selection([{"clip_id": "b1", "narration": "detail"}], candidates, paths)
    assert result[0]["video_name"] == "b.mp4" and result[0]["video_id"] == 2
    assert result[0]["timestamp"] == candidates[0]["timestamp"]
    ranged = candidates + [{"clip_id": "b2", "video_id": 2, "timestamp": "00:00:02,000-00:00:04,000", "picture": "rotation"}]
    result = resolve_selection([{"clip_id": "b1", "end_clip_id": "b2", "narration": "detail"}], ranged, paths)
    assert result[0]["timestamp"] == "00:00:01,000-00:00:04,000"
    for items in ([{"clip_id": "b2", "end_clip_id": "b1"}],
                  [{"clip_id": "b1", "end_clip_id": "b2"}, {"clip_id": "b2"}]):
        try:
            resolve_selection(items, ranged, paths)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid range accepted")
    for invalid in [[], [{"clip_id": "unknown"}], [{"clip_id": "b1"}] * 2]:
        try:
            resolve_selection(invalid, candidates, paths)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid selection accepted")


async def real():
    from app.models.schema import VideoClipParams
    from app.services import task

    paths = ["/NarratoAI/resource/videos/" + name for name in (
        "2026-09-07_224135.MOV", "2026-09-07_230442.MOV", "2026-09-07_224115.MOV")]
    script = await MultiMaterialAnalysisService().generate_documentary_script(
        video_path=paths[0], video_paths=paths, product_mode=True, video_theme="发光玩具商品展示",
        product_description="指尖旋转发光玩具，可组合成光剑，用手指支撑中心旋转把玩。",
        custom_prompt="指尖旋转、解压、发光、光剑、炫酷",
        progress_callback=lambda value, message: print(value, message, flush=True))
    path = Path("/NarratoAI/resource/scripts/product-multi-trial.json")
    path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Selected sources:", [item["video_id"] for item in script], flush=True)
    if "--analyze-only" in sys.argv:
        return
    params = VideoClipParams(video_origin_path=paths[0], video_origin_paths=paths,
        video_clip_json_path=str(path), subtitle_enabled=False, bgm_type="", bgm_name="", n_threads=2,
        tts_engine="indextts", voice_name="")
    task.start_subclip_unified("product-multi-trial", params)


if __name__ == "__main__":
    check()
    if "--real" in sys.argv:
        asyncio.run(real())
    print("PASS")
