"""Run the existing analyzer with a status-aware OpenAI-compatible client."""
import json
import subprocess
import sys

from vision_provider import frame_config, recognize_image
from standardize_analysis import parse_response


def has_audio(video_path):
    """Avoid downloading/loading Whisper for an explicitly silent video."""
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a',
            '-show_entries', 'stream=index', '-of', 'json', str(video_path)],
            check=True, capture_output=True, text=True, timeout=30)
        return bool(json.loads(result.stdout)['streams'])
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return True  # Unknown is not evidence of silence; preserve normal processing.


def generate(prompt, image_path=None, stream=False, model=None, temperature=0.2, num_predict=256):
    try:
        if image_path:
            prompt += '\n只描述当前这一帧，不推测帧间动作、声音或产品功能。忽略此前要求的整片报告结构。返回简短严格JSON：{"visual":"当前帧可见内容","visible_text":["逐字可见字幕"],"uncertainties":["无法确认的内容"]}。不要输出时间，时间由抽帧记录提供。'
        result = recognize_image(image_path, prompt, max_tokens=max(num_predict, 2048) if image_path else num_predict,
                                 temperature=temperature)
        parsed = parse_response(result)
        if image_path and not isinstance(parsed.get("visual"), str):
            raise ValueError("frame response missing visual")
        return {"response": result}
    except (RuntimeError, ValueError) as exc:
        # The analyzer catches Exception and can otherwise save failed frames as success.
        raise SystemExit(str(exc)) from None


def main():
    config = frame_config()
    print("[vision] 帧分析线路：" + config["provider"] + " / " + config["model"], flush=True)
    from video_analyzer import cli
    from video_analyzer.clients.generic_openai_api import GenericOpenAIAPIClient
    from video_analyzer.frame import Frame
    from coverage_sampler import extract
    original_processor = cli.VideoProcessor

    class CoverageProcessor(original_processor):
        def extract_keyframes(self, frames_per_minute=60, duration=None, max_frames=None):
            manifest = extract(self.video_path, self.output_dir, rate=frames_per_minute / 60,
                               hard_limit=max_frames or 240, duration=duration)
            self.frames = [Frame(r["number"], self.output_dir / f'frame_{r["number"]}.jpg',
                                 r["timestamp_seconds"], r["retained_difference"]) for r in manifest["frames"]]
            return self.frames

    cli.VideoProcessor = CoverageProcessor
    original_analyzer = cli.VideoAnalyzer

    class TimestampedAnalyzer(original_analyzer):
        def analyze_frame(self, frame):
            result = super().analyze_frame(frame)
            result.update(timestamp=float(frame.timestamp), frame_number=frame.number)
            return result

    cli.VideoAnalyzer = TimestampedAnalyzer

    if len(sys.argv) > 1 and not sys.argv[1].startswith('-') and not has_audio(sys.argv[1]):
        class SilentAudioProcessor:
            def __init__(self, **kwargs):
                pass

            def extract_audio(self, *args, **kwargs):
                return None

        cli.AudioProcessor = SilentAudioProcessor

    class RoutedClient(GenericOpenAIAPIClient):
        frames_seen = 0

        def generate(self, prompt, image_path=None, **kwargs):
            if config["provider"] == "deepseek" and not image_path and not self.frames_seen:
                raise SystemExit("未获得有效视频帧，无法生成视觉分析；Qwen 视频直连分析已禁用")
            result = generate(prompt, image_path, **kwargs)
            if image_path:
                self.frames_seen += 1
            return result

    # CLI's factory is the single construction point; no installed package edits.
    cli.GenericOpenAIAPIClient = RoutedClient
    sys.argv.extend(["--api-key", config["api_key"], "--api-url", config["api_url"].removesuffix("/chat/completions"),
                     "--model", config["model"]])
    return cli.main()


if __name__ == "__main__":
    main()
