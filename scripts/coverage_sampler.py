"""Coverage-first sampling; local image comparisons only, no model or OCR calls."""
import argparse
import json
import math
from pathlib import Path

import cv2
import numpy as np


def coverage_indices(total, fps, rate=1.0):
    last = (total - 1) / fps
    times = {0.0, last}
    times.update(i / rate for i in range(math.ceil(last * rate) + 1) if i / rate <= last)
    times.update(i / 2 for i in range(7) if i / 2 <= last)
    return sorted({min(total - 1, round(t * fps)) for t in times})


def difference(a, b):
    return float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean())


def extract(video, output, *, rate=1.0, hard_limit=240, duration=None):
    if rate <= 0 or not math.isfinite(rate) or hard_limit < 1:
        raise ValueError("Invalid sampling budget")
    cap = cv2.VideoCapture(str(video))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not cap.isOpened() or not math.isfinite(fps) or fps <= 0 or total <= 0:
            raise ValueError("Cannot read video timing")
        if duration is not None:
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("Invalid processing duration")
            total = min(total, max(1, int(duration * fps)))
        baseline = coverage_indices(total, fps, rate)
        if len(baseline) > hard_limit:
            raise ValueError(f"Coverage needs {len(baseline)} frames, exceeds hard limit {hard_limit}; increase limit explicitly")
        extra_budget = min(math.ceil(len(baseline) * 0.25), hard_limit - len(baseline))
        step = max(1, round(fps / 4))
        candidates = sorted(set(range(0, total, step)) | set(baseline))
        wanted = set(candidates)
        thumbnails = {}
        for index in range(total):
            ok, frame = cap.read()
            if not ok:
                raise ValueError(f"Video decode stopped at frame {index}/{total}")
            if index in wanted:
                thumbnails[index] = cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2GRAY)
        adjacent = {n: (difference(thumbnails[n], thumbnails[candidates[i-1]]) if i else 0.0)
                    for i, n in enumerate(candidates)}
        # At most one extra per coverage interval; cumulative change also counts.
        proposals = []
        threshold = 10.0
        min_distance = max(1, round(fps * 0.2))
        for left, right in zip(baseline, baseline[1:]):
            options = [n for n in candidates if left + min_distance <= n <= right - min_distance]
            if options:
                scored = [(max(adjacent[n], difference(thumbnails[n], thumbnails[left])), n) for n in options]
                score, index = max(scored)
                if score > threshold:
                    proposals.append((score, index))
        extras = {n for _, n in sorted(proposals, reverse=True)[:extra_budget]}
        selected = sorted(set(baseline) | extras)
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        records = []
        previous = None
        for number, index in enumerate(selected):
            cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = cap.read()
            if not ok:
                raise ValueError(f"Cannot decode selected frame {index}")
            path = output / f"frame_{number}.jpg"
            if not cv2.imwrite(str(path), frame):
                raise OSError(f"Cannot save {path}")
            reason = "endpoint" if index in (0, total - 1) else ("difference_bonus" if index in extras else "time_coverage")
            records.append({"number": number, "source_frame": index, "timestamp_seconds": index / fps,
                "path": str(path), "reason": reason, "adjacent_difference": adjacent[index],
                "retained_difference": difference(thumbnails[index], thumbnails[previous]) if previous is not None else 0.0})
            previous = index
        gaps = [(b-a)/fps for a,b in zip(selected, selected[1:])]
        allowed_gap = max(1 / rate, 0.5) + 1 / fps
        coverage_ok = selected[0] == 0 and selected[-1] == total - 1 and max(gaps, default=0) <= allowed_gap
        manifest = {"sampler_version": 1, "duration_seconds": total / fps, "fps": fps,
            "baseline_count": len(baseline), "extra_budget": extra_budget, "hard_limit": hard_limit,
            "selected_count": len(records), "candidate_count": len(candidates),
            "eligible_extra_count": len(proposals), "discarded_extra_count": len(proposals)-len(extras),
            "difference_threshold": threshold, "base_rate": rate, "coverage_ok": coverage_ok,
            "max_gap_seconds": max(gaps, default=0), "first_timestamp": selected[0]/fps,
            "last_timestamp": selected[-1]/fps, "tail_gap_seconds": (total-selected[-1])/fps,
            "frames": records,
            "candidates": [{"source_frame": n, "timestamp_seconds": n/fps,
                            "adjacent_difference": adjacent[n], "selected": n in selected} for n in candidates]}
        (output.parent / "sampling_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if not coverage_ok:
            raise ValueError("Sampling coverage validation failed")
        return manifest
    finally:
        cap.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("output")
    args = parser.parse_args()
    result = extract(args.video, args.output)
    print(json.dumps({k:v for k,v in result.items() if k not in ("frames", "candidates")}, ensure_ascii=False))
