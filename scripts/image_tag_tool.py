"""PNG to JPEG conversion with a verified XMP subject tag."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import BinaryIO
from xml.etree import ElementTree
from xml.sax.saxutils import escape as xml_escape


MAX_IMAGE_PIXELS = 50_000_000
MAX_TAG_LENGTH = 200
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
DC_NS = "http://purl.org/dc/elements/1.1/"


class ImageTagToolError(ValueError):
    """Raised when an uploaded image cannot be converted safely."""


def normalize_tag(value: str) -> str:
    tag = str(value or "").strip()
    if not tag:
        raise ImageTagToolError("请输入标签")
    if len(tag) > MAX_TAG_LENGTH:
        raise ImageTagToolError(f"标签不能超过 {MAX_TAG_LENGTH} 个字符")
    if any(ord(char) < 32 for char in tag):
        raise ImageTagToolError("标签不能包含控制字符")
    return tag


def build_subject_xmp(tag: str) -> bytes:
    """Build the XMP dc:subject rdf:Bag structure required by the upload flow."""
    escaped_tag = xml_escape(normalize_tag(tag))
    return (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        f'<rdf:RDF xmlns:rdf="{RDF_NS}">\n'
        f'<rdf:Description rdf:about="" xmlns:dc="{DC_NS}">\n'
        "<dc:subject><rdf:Bag>"
        f"<rdf:li>{escaped_tag}</rdf:li>"
        "</rdf:Bag></dc:subject>\n"
        "</rdf:Description>\n"
        "</rdf:RDF>\n"
        "</x:xmpmeta>\n"
        '<?xpacket end="w"?>'
    ).encode("utf-8")


def xmp_contains_subject(xmp: bytes | str | None, tag: str) -> bool:
    if not xmp:
        return False
    try:
        root = ElementTree.fromstring(xmp)
    except (ElementTree.ParseError, TypeError, ValueError):
        return False
    subject_path = f".//{{{DC_NS}}}subject/{{{RDF_NS}}}Bag/{{{RDF_NS}}}li"
    return any((item.text or "") == tag for item in root.findall(subject_path))


def convert_png_to_jpeg(source: BinaryIO, output_path: Path, tag: str) -> tuple[int, int]:
    """Convert one static PNG to JPEG, then verify the exact XMP subject tag."""
    from PIL import Image, UnidentifiedImageError

    tag = normalize_tag(tag)
    if source is None or not hasattr(source, "seek"):
        raise ImageTagToolError("无法读取 PNG：文件内容为空")
    converted = None
    try:
        source.seek(0)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                if image.format != "PNG":
                    raise ImageTagToolError("仅支持 PNG 图片")
                if getattr(image, "is_animated", False):
                    raise ImageTagToolError("不支持 APNG 动图")
                width, height = image.size
                if width * height > MAX_IMAGE_PIXELS:
                    raise ImageTagToolError("图片像素超过 5000 万限制")
                image.load()
                icc_profile = image.info.get("icc_profile")
                has_alpha = image.mode in {"LA", "RGBA"} or "transparency" in image.info
                if has_alpha:
                    rgba = image.convert("RGBA")
                    white = Image.new("RGBA", rgba.size, "white")
                    white.alpha_composite(rgba)
                    converted = white.convert("RGB")
                else:
                    converted = image.convert("RGB")
    except ImageTagToolError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError, EOFError, SyntaxError) as exc:
        raise ImageTagToolError(f"无法读取 PNG：{exc}") from exc

    save_kwargs: dict[str, object] = {
        "format": "JPEG",
        "quality": 95,
        "xmp": build_subject_xmp(tag),
    }
    if isinstance(icc_profile, bytes):
        save_kwargs["icc_profile"] = icc_profile
    try:
        converted.save(output_path, **save_kwargs)
        with Image.open(output_path) as rendered:
            if rendered.format != "JPEG" or not xmp_contains_subject(rendered.info.get("xmp"), tag):
                raise ImageTagToolError("JPG 标签写入校验失败")
    except ImageTagToolError:
        raise
    except (OSError, ValueError) as exc:
        raise ImageTagToolError(f"无法生成 JPG：{exc}") from exc
    finally:
        if converted is not None:
            converted.close()
    return width, height
