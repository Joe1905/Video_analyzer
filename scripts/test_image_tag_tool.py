#!/usr/bin/env python3
"""Ad-hoc verification for the PNG XMP tagging helper."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from image_tag_tool import ImageTagToolError, build_subject_xmp, convert_png_to_jpeg, xmp_contains_subject


class ImageTagToolTest(unittest.TestCase):
    def make_png(self, mode: str = "RGB") -> io.BytesIO:
        image = Image.new(mode, (3, 2), (10, 20, 30, 128) if "A" in mode else (10, 20, 30))
        stream = io.BytesIO()
        image.save(stream, "PNG")
        image.close()
        stream.seek(0)
        return stream

    def test_writes_subject_xmp_to_jpeg(self) -> None:
        tag = "contains-synthetic-performer"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "image.jpg"
            self.assertEqual(convert_png_to_jpeg(self.make_png(), target, tag), (3, 2))
            with Image.open(target) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertTrue(xmp_contains_subject(image.info.get("xmp"), tag))

    def test_transparent_png_uses_white_background(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "transparent.jpg"
            convert_png_to_jpeg(self.make_png("RGBA"), target, "tag")
            with Image.open(target) as image:
                pixel = image.convert("RGB").getpixel((0, 0))
            self.assertGreater(pixel[0], 100)
            self.assertGreater(pixel[1], 100)
            self.assertGreater(pixel[2], 100)

    def test_rejects_non_png_and_animated_png(self) -> None:
        jpg = io.BytesIO()
        Image.new("RGB", (1, 1), "red").save(jpg, "JPEG")
        jpg.seek(0)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "invalid.jpg"
            with self.assertRaisesRegex(ImageTagToolError, "仅支持 PNG"):
                convert_png_to_jpeg(jpg, target, "tag")
            animated = io.BytesIO()
            first = Image.new("RGB", (1, 1), "red")
            second = Image.new("RGB", (1, 1), "blue")
            first.save(animated, "PNG", save_all=True, append_images=[second])
            animated.seek(0)
            with self.assertRaisesRegex(ImageTagToolError, "APNG"):
                convert_png_to_jpeg(animated, target, "tag")
            with self.assertRaisesRegex(ImageTagToolError, "无法读取 PNG"):
                convert_png_to_jpeg(io.BytesIO(b"not a PNG"), target, "tag")

    def test_escapes_and_validates_tags(self) -> None:
        tag = "AI & <person>"
        self.assertTrue(xmp_contains_subject(build_subject_xmp(tag), tag))
        with self.assertRaises(ImageTagToolError):
            build_subject_xmp(" \n ")


if __name__ == "__main__":
    unittest.main()
