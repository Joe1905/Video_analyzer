"""Registration for the cached Taobao page."""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from core.http import binary_response, file_response, json_response, text_response
from routes.router import Router


def register_taobao_page(
    router: Router,
    *,
    html_snapshot: str,
    inject_nav: Callable[[str, str], str],
) -> None:
    """Register the Taobao page using its import-time HTML snapshot."""

    def taobao_page(handler: Any, params: Mapping[str, str]) -> None:
        text_response(
            handler,
            HTTPStatus.OK,
            inject_nav(html_snapshot, "/taobao"),
            "text/html; charset=utf-8",
        )

    router.get("/taobao", taobao_page)


def register_taobao_api_routes(
    router: Router,
    *,
    collector: Any,
    current_global_user: Callable[[Any], dict[str, Any]],
    guess_type: Callable[[str], tuple[str | None, str | None]],
) -> Callable[[Any], None]:
    """Register the Taobao API with its collector kept as the domain core."""

    archive_id_pattern = r"[0-9]{14}-[a-f0-9]{8}"
    filename_pattern = r"[A-Za-z0-9._-]+"

    def current_user(handler: Any) -> dict[str, Any]:
        return current_global_user(handler)

    def state(handler: Any, _params: Mapping[str, str]) -> None:
        user = current_user(handler)
        try:
            json_response(handler, HTTPStatus.OK, collector.state(user))
        except FileNotFoundError:
            json_response(handler, HTTPStatus.NOT_FOUND, {"error": "归档文件不存在"})
        except ValueError as exc:
            json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def archives(handler: Any, _params: Mapping[str, str]) -> None:
        user = current_user(handler)
        try:
            json_response(handler, HTTPStatus.OK, {"archives": collector.list_archives(user)})
        except FileNotFoundError:
            json_response(handler, HTTPStatus.NOT_FOUND, {"error": "归档文件不存在"})
        except ValueError as exc:
            json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def export(handler: Any, params: Mapping[str, str]) -> None:
        user = current_user(handler)
        try:
            archive_id = params["archive_id"]
            if not re.fullmatch(archive_id_pattern, archive_id):
                return json_response(handler, HTTPStatus.NOT_FOUND, {"error": "Not found"})
            requested_format = str(parse_qs(urlparse(handler.path).query).get("format", ["json"])[0]).lower()
            if requested_format == "md":
                return binary_response(
                    handler,
                    HTTPStatus.OK,
                    collector.export_markdown(user, archive_id).encode("utf-8"),
                    "text/markdown; charset=utf-8",
                    f"taobao-{archive_id}.md",
                    "no-store",
                )
            if requested_format == "json":
                archive = collector.archive_path(user, archive_id, "metadata.json")
                return file_response(
                    handler,
                    archive,
                    "application/json; charset=utf-8",
                    f"taobao-{archive_id}.json",
                    archive.stat().st_size,
                )
            raise ValueError("导出格式仅支持 json 或 md")
        except FileNotFoundError:
            return json_response(handler, HTTPStatus.NOT_FOUND, {"error": "归档文件不存在"})
        except ValueError as exc:
            return json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            return json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def archive_file(handler: Any, params: Mapping[str, str]) -> None:
        user = current_user(handler)
        try:
            archive_id = params["archive_id"]
            filename = params["filename"]
            if not re.fullmatch(archive_id_pattern, archive_id) or not re.fullmatch(filename_pattern, filename):
                return json_response(handler, HTTPStatus.NOT_FOUND, {"error": "Not found"})
            archive = collector.archive_path(user, archive_id, filename)
            content_type = guess_type(archive.name)[0] or "application/octet-stream"
            return file_response(
                handler,
                archive,
                content_type,
                archive.name,
                archive.stat().st_size,
                download=archive.suffix.lower() in {".html", ".json"},
            )
        except FileNotFoundError:
            return json_response(handler, HTTPStatus.NOT_FOUND, {"error": "归档文件不存在"})
        except ValueError as exc:
            return json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            return json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def unknown_get(handler: Any, _params: Mapping[str, str]) -> None:
        current_user(handler)
        json_response(handler, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def post(handler: Any, _params: Mapping[str, str]) -> None:
        user = current_user(handler)
        try:
            payload = handler.read_json_body()
            path = urlparse(handler.path).path
            if path == "/api/taobao/session/start":
                return json_response(handler, HTTPStatus.OK, collector.start_session(user))
            if path == "/api/taobao/session/stop":
                return json_response(handler, HTTPStatus.OK, collector.stop_session(user))
            if path == "/api/taobao/session/open-login":
                return json_response(handler, HTTPStatus.OK, collector.open_login(user))
            if path == "/api/taobao/collect":
                return json_response(
                    handler,
                    HTTPStatus.OK,
                    collector.collect(user, payload.get("keyword"), payload.get("url")),
                )
            return json_response(handler, HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except ValueError as exc:
            return json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            return json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    router.get("/api/taobao/state", state)
    router.get("/api/taobao/archives", archives)
    router.get("/api/taobao/archives/{archive_id}/export", export)
    router.get("/api/taobao/archives/{archive_id}/{filename}", archive_file)
    router.get_prefix("/api/taobao/", unknown_get)
    router.post("/api/taobao/session/start", post)
    router.post("/api/taobao/session/stop", post)
    router.post("/api/taobao/session/open-login", post)
    router.post("/api/taobao/collect", post)

    def unknown_post(handler: Any) -> None:
        post(handler, {})

    return unknown_post
