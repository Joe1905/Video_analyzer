# Frame sampling

The frame analyzer uses `coverage_sampler.py` before calling a vision model.
It does not run OCR or use subtitle changes to choose frames.

- The configured frame rate (`frames.per_minute`, normally 60) determines linear coverage anchors.
- The first and last decoded frames are mandatory; the first three seconds also receive half-second anchors.
- Local grayscale comparisons use 160x90 thumbnails. Both adjacent-candidate change and accumulated change since the preceding coverage anchor are considered.
- Each coverage interval may nominate one extra frame. Extras may add at most 25% (rounded up) to the coverage count. They cannot evict coverage anchors.
- `analyze_one.sh` now uses `FRAME_HARD_LIMIT` (default 240), rather than the old `MAX_FRAMES=20` cutoff. Pass this variable into the analyzer container for an override. If mandatory coverage alone exceeds the limit, the task fails explicitly; reduce scope or raise the limit deliberately.
- `sampling_manifest.json` records all candidate times/scores, selected frames/reasons, budgets, rejected extra count, first/last timestamps and maximum gap. It is generated data, not source code.
- Standardization keeps response completeness and sampling coverage separate, and verifies that extracted evidence timestamps match the manifest. A complete response is not proof of full motion understanding; intervals between sampled frames remain unobserved.

Validation in the development Compose project:

```bash
docker-compose -p short-video-analyzer-dev exec -T web python scripts/test_coverage_sampler.py
docker-compose -p short-video-analyzer-dev exec -T web python scripts/test_extraction_evidence.py
```

For a local sampling-only run (no ASR or model call), run `python scripts/coverage_sampler.py videos/example.mp4 output/sampling-preview/example/frames` inside the same Compose environment.
