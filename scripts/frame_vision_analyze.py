"""Run the existing analyzer with a status-aware OpenAI-compatible client."""
import json
import subprocess
import sys

from vision_provider import frame_config, recognize_image


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
        return {"response": recognize_image(image_path, prompt, max_tokens=num_predict,
                                            temperature=temperature)}
    except RuntimeError as exc:
        # The analyzer catches Exception and can otherwise save failed frames as success.
        raise SystemExit(str(exc)) from None


def main():
    config = frame_config()
    print("[vision] 帧分析线路：" + config["provider"] + " / " + config["model"], flush=True)
    from video_analyzer import cli
    from video_analyzer.clients.generic_openai_api import GenericOpenAIAPIClient

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
