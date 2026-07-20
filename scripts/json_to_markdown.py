#!/usr/bin/env python3
"""Deterministically render JSON-compatible data as lossless Markdown.

The renderer is intentionally domain-neutral.  It preserves source field names and
JSON paths, does not calculate metrics, and never silently drops rows or values.
FastMoss responses are recognised only to separate their transport metadata from
the business ``data`` payload; payload fields are rendered by the same generic
rules as any other JSON document.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Sequence


JSONScalar = str | int | float | bool | None
JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

_ENVELOPE_FIELDS = ("code", "msg", "message", "timestamp", "request_id")
_SIMPLE_PATH_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _validate_json_value(value: Any, path: str = "$") -> None:
    if _is_scalar(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key: {key!r}")
            _validate_json_value(item, _child_path(path, key))
        return
    raise ValueError(f"{path} contains a non-JSON value of type {type(value).__name__}")


def _child_path(path: str, key: str) -> str:
    if _SIMPLE_PATH_KEY.fullmatch(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def _escape_cell(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\r", "<br>")
        .replace("\n", "<br>")
    )


def _escape_heading(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().replace("#", "\\#") or "(空字段名)"


def _scalar_text(value: JSONScalar) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        if value == "":
            return '""'
        if value.isspace():
            return json.dumps(value, ensure_ascii=False)
        return value
    if isinstance(value, float):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    return str(value)


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    escaped_headers = [_escape_cell(str(item)) for item in headers]
    lines = [
        "| " + " | ".join(escaped_headers) + " |",
        "| " + " | ".join("---" for _ in escaped_headers) + " |",
    ]
    for row in rows:
        cells = [_escape_cell(str(item)) for item in row]
        if len(cells) < len(escaped_headers):
            cells.extend("" for _ in range(len(escaped_headers) - len(cells)))
        lines.append("| " + " | ".join(cells[: len(escaped_headers)]) + " |")
    return lines


class JSONMarkdownRenderer:
    """Render JSON values without changing their facts or business semantics."""

    def __init__(
        self,
        *,
        include_paths: bool = True,
        max_table_rows: int | None = None,
        table_max_columns: int = 12,
    ) -> None:
        if max_table_rows is not None and max_table_rows < 1:
            raise ValueError("max_table_rows must be at least 1 or None")
        if table_max_columns < 1:
            raise ValueError("table_max_columns must be at least 1")
        self.include_paths = include_paths
        self.max_table_rows = max_table_rows
        self.table_max_columns = table_max_columns

    def render(self, value: JSONValue, *, title: str | None = None) -> str:
        _validate_json_value(value)
        lines = [f"# {_escape_heading(title or 'JSON 数据')}"]

        if self._is_fastmoss_envelope(value):
            assert isinstance(value, dict)
            metadata = [(key, value[key]) for key in _ENVELOPE_FIELDS if key in value]
            if metadata:
                lines.extend(["", "## 响应元数据", ""])
                rows = []
                for key, item in metadata:
                    row = [key, _scalar_text(item)]
                    if self.include_paths:
                        row.append(_child_path("$", key))
                    rows.append(row)
                headers = ["字段", "值"] + (["JSON 路径"] if self.include_paths else [])
                lines.extend(_table(headers, rows))

            lines.extend(["", self._heading(2, "data", "$.data"), ""])
            lines.extend(self._render_value(value["data"], "$.data", 3))

            extra_keys = [
                key for key in value if key not in {*_ENVELOPE_FIELDS, "data"}
            ]
            if extra_keys:
                lines.extend(["", "## 其他顶层字段", ""])
                lines.extend(
                    self._render_dict({key: value[key] for key in extra_keys}, "$", 3)
                )
        else:
            lines.extend(["", *self._render_value(value, "$", 2)])

        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _is_fastmoss_envelope(value: JSONValue) -> bool:
        return (
            isinstance(value, dict)
            and "data" in value
            and any(key in value for key in _ENVELOPE_FIELDS)
        )

    def _heading(self, level: int, label: str, path: str) -> str:
        text = f"{'#' * min(level, 6)} {_escape_heading(label)}"
        if self.include_paths:
            text += f" (`{path}`)"
        return text

    def _render_value(self, value: JSONValue, path: str, level: int) -> list[str]:
        if _is_scalar(value):
            row = ["值", _scalar_text(value)]
            headers = ["类型", "内容"]
            if self.include_paths:
                headers.append("JSON 路径")
                row.append(path)
            return _table(headers, [row])
        if isinstance(value, dict):
            if not value:
                return [f"空对象{{}}" + (f"（`{path}`）" if self.include_paths else "")]
            return self._render_dict(value, path, level)
        if not value:
            return [f"空数组[]" + (f"（`{path}`）" if self.include_paths else "")]
        return self._render_list(value, path, level)

    def _render_dict(self, value: dict[str, JSONValue], path: str, level: int) -> list[str]:
        lines: list[str] = []
        scalar_items = [(key, item) for key, item in value.items() if _is_scalar(item)]
        if scalar_items:
            rows = []
            for key, item in scalar_items:
                row = [key, _scalar_text(item)]
                if self.include_paths:
                    row.append(_child_path(path, key))
                rows.append(row)
            headers = ["字段", "值"] + (["JSON 路径"] if self.include_paths else [])
            lines.extend(_table(headers, rows))

        for key, item in value.items():
            if _is_scalar(item):
                continue
            if lines:
                lines.append("")
            child_path = _child_path(path, key)
            lines.extend(
                [self._heading(level, key, child_path), "", *self._render_value(item, child_path, level + 1)]
            )
        return lines

    def _render_list(self, value: list[JSONValue], path: str, level: int) -> list[str]:
        included, omitted = self._visible_rows(value)
        summary = self._list_summary(len(value), len(included), omitted)
        if all(_is_scalar(item) for item in included):
            rows = []
            for index, item in enumerate(included):
                row = [str(index), _scalar_text(item)]
                if self.include_paths:
                    row.append(f"{path}[{index}]")
                rows.append(row)
            headers = ["序号", "值"] + (["JSON 路径"] if self.include_paths else [])
            lines = [summary, "", *_table(headers, rows)]
            return self._append_omission(lines, omitted, len(included), len(value), path)

        if self._can_render_object_table(included):
            keys = self._ordered_keys(included)
            headers = ["序号", *keys]
            if self.include_paths:
                headers.append("JSON 路径")
            rows = []
            for index, item in enumerate(included):
                assert isinstance(item, dict)
                row = [str(index)]
                for key in keys:
                    row.append(_scalar_text(item[key]) if key in item else "（字段缺失）")
                if self.include_paths:
                    row.append(f"{path}[{index}]")
                rows.append(row)
            lines = [summary, "", *_table(headers, rows)]
            return self._append_omission(lines, omitted, len(included), len(value), path)

        lines: list[str] = [summary]
        for index, item in enumerate(included):
            if lines:
                lines.append("")
            item_path = f"{path}[{index}]"
            lines.extend(
                [
                    self._heading(level, f"项目 {index + 1}", item_path),
                    "",
                    *self._render_value(item, item_path, level + 1),
                ]
            )
        return self._append_omission(lines, omitted, len(included), len(value), path)

    @staticmethod
    def _list_summary(total: int, included: int, omitted: int) -> str:
        if omitted:
            return f"数组，共 {total} 项；本次展示前 {included} 项。"
        return f"数组，共 {total} 项；以下完整展示全部 {included} 项。"

    def _visible_rows(self, value: list[JSONValue]) -> tuple[list[JSONValue], int]:
        if self.max_table_rows is None or len(value) <= self.max_table_rows:
            return value, 0
        return value[: self.max_table_rows], len(value) - self.max_table_rows

    def _can_render_object_table(self, value: list[JSONValue]) -> bool:
        if not value or not all(isinstance(item, dict) for item in value):
            return False
        keys = self._ordered_keys(value)
        return len(keys) <= self.table_max_columns and all(
            _is_scalar(child)
            for item in value
            for child in item.values()  # type: ignore[union-attr]
        )

    @staticmethod
    def _ordered_keys(value: list[JSONValue]) -> list[str]:
        keys: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            for key in item:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        return keys

    @staticmethod
    def _append_omission(
        lines: list[str], omitted: int, included: int, total: int, path: str
    ) -> list[str]:
        if omitted:
            lines.extend(
                [
                    "",
                    f"> 显式裁剪：`{path}` 共 {total} 项，本次展示 {included} 项，省略 {omitted} 项。",
                ]
            )
        return lines


def json_to_markdown(
    value: JSONValue,
    *,
    title: str | None = None,
    include_paths: bool = True,
    max_table_rows: int | None = None,
    table_max_columns: int = 12,
) -> str:
    """Render an already-decoded JSON-compatible value as Markdown."""

    return JSONMarkdownRenderer(
        include_paths=include_paths,
        max_table_rows=max_table_rows,
        table_max_columns=table_max_columns,
    ).render(value, title=title)


def json_text_to_markdown(text: str, **kwargs: Any) -> str:
    """Parse a JSON document and render it as Markdown."""

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    return json_to_markdown(value, **kwargs)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render JSON as deterministic Markdown")
    parser.add_argument("input", nargs="?", help="JSON file; omit or use - to read stdin")
    parser.add_argument("-o", "--output", help="Markdown output file; defaults to stdout")
    parser.add_argument("--title", help="Markdown document title")
    parser.add_argument("--no-paths", action="store_true", help="omit JSON path columns")
    parser.add_argument(
        "--max-table-rows",
        type=int,
        help="explicitly limit displayed list rows (default: unlimited)",
    )
    parser.add_argument(
        "--table-max-columns",
        type=int,
        default=12,
        help="maximum object fields before rendering records as sections (default: 12)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.input and args.input != "-":
        text = Path(args.input).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    try:
        markdown = json_text_to_markdown(
            text,
            title=args.title,
            include_paths=not args.no_paths,
            max_table_rows=args.max_table_rows,
            table_max_columns=args.table_max_columns,
        )
    except ValueError as exc:
        print(f"json-to-markdown: {exc}", file=sys.stderr)
        return 2
    if args.output:
        Path(args.output).write_text(markdown, encoding="utf-8")
    else:
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
