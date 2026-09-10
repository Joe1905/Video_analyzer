"""Run in the trial container; --real uses the three uploaded product videos and live models."""
import asyncio
import json
import sys
from pathlib import Path

from app.services.narrato_multi_material import MultiMaterialAnalysisService, resolve_selection


def check():
    paths = ["/tmp/a.mp4", "/tmp/b.mp4"]
    candidates = [{"clip_id": "b1", "video_id": 2, "timestamp": "00:00:01,000-00:00:02,000", "picture": "detail"}]
    result = resolve_selection([{"clip_id": "b1", "narration": "detail"}], candidates, paths)
    assert result[0]["video_name"] == "b.mp4" and result[0]["video_id"] == 2
    assert result[0]["timestamp"] == candidates[0]["timestamp"]
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
        progress_callback=lambda value, message: print(value, message, flush=True))
    path = Path("/NarratoAI/resource/scripts/product-multi-trial.json")
    path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Selected sources:", [item["video_id"] for item in script], flush=True)
    params = VideoClipParams(video_origin_path=paths[0], video_origin_paths=paths,
        video_clip_json_path=str(path), subtitle_enabled=False, bgm_type="", bgm_name="", n_threads=2,
        tts_engine="indextts", voice_name="")
    task.start_subclip_unified("product-multi-trial", params)


if __name__ == "__main__":
    check()
    if "--real" in sys.argv:
        asyncio.run(real())
    print("PASS")
