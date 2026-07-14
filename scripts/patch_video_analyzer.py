#!/usr/bin/env python3
"""Apply deterministic runtime fixes to the pinned video-analyzer package."""
from __future__ import annotations

import os
import re
from pathlib import Path

import video_analyzer


PACKAGE_DIR = Path(video_analyzer.__file__).resolve().parent


def replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    if source.count(old) != 1:
        raise RuntimeError(f"expected one patch target in {path}: {old[:80]!r}")
    path.write_text(source.replace(old, new), encoding="utf-8")


def replace_method(path: Path, name: str, next_name: str, body: str) -> None:
    source = path.read_text(encoding="utf-8")
    pattern = rf"    def {name}\(.*?(?=    def {next_name}\()"
    updated, count = re.subn(pattern, body.rstrip() + "\n\n", source, count=1, flags=re.DOTALL)
    if count != 1:
        raise RuntimeError(f"method patch target not found: {path}:{name}")
    path.write_text(updated, encoding="utf-8")


def patch_client() -> None:
    path = PACKAGE_DIR / "clients" / "generic_openai_api.py"
    replace_once(
        path,
        "return {\"response\": message['content']}",
        "return {\"response\": message['content'], \"finish_reason\": "
        "json_response['choices'][0].get('finish_reason'), \"usage\": json_response.get('usage') or {}}",
    )


def patch_analyzer() -> None:
    path = PACKAGE_DIR / "analyzer.py"
    replace_once(path, "import logging\n", "import logging\nimport os\n")
    replace_method(
        path,
        "_format_previous_analyses",
        "analyze_frame",
        '''    def _format_previous_analyses(self) -> str:
        """Format a bounded window of previous frame analyses."""
        if not self.previous_analyses:
            return ""
        window = max(0, int(os.getenv("FRAME_CONTEXT_WINDOW", "3")))
        if window == 0:
            return ""
        selected = self.previous_analyses[-window:]
        start_index = len(self.previous_analyses) - len(selected)
        formatted_analyses = []
        for i, analysis in enumerate(selected, start=start_index):
            formatted_analyses.append(
                f"Frame {i}\n{analysis.get('response', 'No analysis available')}\n"
            )
        return "\n".join(formatted_analyses)''',
    )
    replace_method(
        path,
        "analyze_frame",
        "reconstruct_video",
        '''    def analyze_frame(self, frame: Frame) -> Dict[str, Any]:
        """Analyze one frame and preserve its authoritative timestamp."""
        prompt = self.frame_prompt.replace("{PREVIOUS_FRAMES}", self._format_previous_analyses())
        prompt = prompt.replace("{prompt}", self._format_user_prompt())
        prompt = f"{prompt}\n This is frame {frame.number} captured at {frame.timestamp:.2f} seconds."
        initial_tokens = max(300, int(os.getenv("FRAME_ANALYSIS_MAX_TOKENS", "1200")))
        retry_tokens = max(initial_tokens, int(os.getenv("FRAME_ANALYSIS_RETRY_MAX_TOKENS", "2400")))
        try:
            response = self.client.generate(
                prompt=prompt,
                image_path=str(frame.path),
                model=self.model,
                temperature=self.temperature,
                num_predict=initial_tokens,
            )
            if response.get("finish_reason") in {"length", "max_tokens"} and retry_tokens > initial_tokens:
                logger.warning("Frame %s was truncated; retrying with %s tokens", frame.number, retry_tokens)
                response = self.client.generate(
                    prompt=prompt,
                    image_path=str(frame.path),
                    model=self.model,
                    temperature=self.temperature,
                    num_predict=retry_tokens,
                )
            if response.get("finish_reason") in {"length", "max_tokens"}:
                raise RuntimeError(f"frame {frame.number} analysis remained truncated after retry")
            analysis_result = {k: v for k, v in response.items() if k != "context"}
            analysis_result["frame_number"] = int(frame.number)
            analysis_result["timestamp_seconds"] = round(float(frame.timestamp), 3)
            self.previous_analyses.append(analysis_result)
            logger.debug("Successfully analyzed frame %s", frame.number)
            return analysis_result
        except Exception as exc:
            logger.error("Error analyzing frame %s: %s", frame.number, exc)
            raise RuntimeError(f"Error analyzing frame {frame.number}: {exc}") from exc''',
    )
    source = path.read_text(encoding="utf-8")
    old = '''            response = self.client.generate(
                prompt=prompt,
                model=self.model,
                temperature=self.temperature,
                num_predict=1000
            )
            logger.info("Successfully reconstructed video description")
            return {k: v for k, v in response.items() if k != "context"}'''
    new = '''            initial_tokens = max(1000, int(os.getenv("VIDEO_RECONSTRUCTION_MAX_TOKENS", "8192")))
            retry_tokens = max(initial_tokens, int(os.getenv("VIDEO_RECONSTRUCTION_RETRY_MAX_TOKENS", "12000")))
            response = self.client.generate(
                prompt=prompt,
                model=self.model,
                temperature=self.temperature,
                num_predict=initial_tokens
            )
            if response.get("finish_reason") in {"length", "max_tokens"} and retry_tokens > initial_tokens:
                logger.warning("Video reconstruction was truncated; retrying with %s tokens", retry_tokens)
                response = self.client.generate(
                    prompt=prompt,
                    model=self.model,
                    temperature=self.temperature,
                    num_predict=retry_tokens
                )
            if response.get("finish_reason") in {"length", "max_tokens"}:
                raise RuntimeError("video reconstruction remained truncated after retry")
            logger.info("Successfully reconstructed video description")
            return {k: v for k, v in response.items() if k != "context"}'''
    if source.count(old) != 1:
        raise RuntimeError(f"reconstruction patch target not found: {path}")
    source = source.replace(old, new)
    old_error = '''        except Exception as e:
            logger.error(f"Error reconstructing video: {e}")
            return {"response": f"Error reconstructing video: {str(e)}"}'''
    new_error = '''        except Exception as e:
            logger.error(f"Error reconstructing video: {e}")
            raise RuntimeError(f"Error reconstructing video: {e}") from e'''
    if source.count(old_error) != 1:
        raise RuntimeError(f"reconstruction error patch target not found: {path}")
    path.write_text(source.replace(old_error, new_error), encoding="utf-8")


def patch_frame_processor() -> None:
    path = PACKAGE_DIR / "frame.py"
    replace_once(path, "import logging\n", "import logging\nimport os\n")
    source = path.read_text(encoding="utf-8")
    pattern = r"    def extract_keyframes\(.*\Z"
    method = '''    def extract_keyframes(self, frames_per_minute: int = 10, duration: Optional[float] = None,
                          max_frames: Optional[int] = None) -> List[Frame]:
        """Extract chronologically distributed frames with mandatory time anchors."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        cap = cv2.VideoCapture(str(self.video_path))
        if not cap.isOpened():
            raise ValueError(f"Could not open video file: {self.video_path}")
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if fps <= 0 or total_frames <= 0:
            cap.release()
            raise ValueError(f"Invalid video metadata: fps={fps}, frames={total_frames}")
        video_duration = total_frames / fps
        if duration:
            video_duration = min(float(duration), video_duration)
            total_frames = min(total_frames, max(1, int(video_duration * fps)))
        requested_max = max_frames if max_frames is not None else total_frames
        target_frames = max(1, min(int((video_duration / 60) * frames_per_minute), total_frames, requested_max))
        anchor_seconds = max(1.0, float(os.getenv("LONG_VIDEO_FRAME_INTERVAL_SECONDS", "10")))
        anchor_step = max(1, int(round(anchor_seconds * fps)))
        mandatory = set(range(0, total_frames, anchor_step))
        mandatory.add(total_frames - 1)
        if len(mandatory) > target_frames:
            mandatory = set(sorted(mandatory)[: max(0, target_frames - 1)]) | {total_frames - 1}

        sample_interval = max(1, total_frames // max(1, target_frames * 3))
        scored = []
        previous = None
        frame_number = 0
        while frame_number < total_frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
            ok, image = cap.read()
            if not ok:
                break
            score = self._calculate_frame_difference(image, previous)
            scored.append((frame_number, score))
            previous = image
            frame_number += sample_interval

        selected = set(mandatory)
        for frame_number, _score in sorted(scored, key=lambda row: row[1], reverse=True):
            if len(selected) >= target_frames:
                break
            if frame_number not in selected:
                selected.add(frame_number)
        if len(selected) < target_frames:
            for frame_number in np.linspace(0, total_frames - 1, target_frames, dtype=int):
                selected.add(int(frame_number))
                if len(selected) >= target_frames:
                    break

        score_by_frame = dict(scored)
        selected_numbers = sorted(selected)[:target_frames]
        self.frames = []
        for index, frame_number in enumerate(selected_numbers):
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
            ok, image = cap.read()
            if not ok:
                cap.release()
                raise RuntimeError(f"Could not read selected frame {frame_number}")
            frame_path = self.output_dir / f"frame_{index}.jpg"
            if not cv2.imwrite(str(frame_path), image):
                cap.release()
                raise RuntimeError(f"Could not write selected frame {frame_path}")
            self.frames.append(Frame(index, frame_path, frame_number / fps, float(score_by_frame.get(frame_number, 0.0))))
        cap.release()
        logger.info("Extracted %s frames from video (target was %s)", len(self.frames), target_frames)
        return self.frames
'''
    updated, count = re.subn(pattern, method, source, count=1, flags=re.DOTALL)
    if count != 1:
        raise RuntimeError(f"frame processor patch target not found: {path}")
    path.write_text(updated, encoding="utf-8")


def main() -> None:
    patch_client()
    patch_analyzer()
    patch_frame_processor()
    print(f"patched pinned video_analyzer package at {PACKAGE_DIR}")


if __name__ == "__main__":
    main()
