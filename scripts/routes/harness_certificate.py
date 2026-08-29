"""Registration for the fixed Harness certificate download."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import Any, Mapping

from core.http import text_response
from routes.router import Router


CERTIFICATE_ROUTE = "/harness-ca.crt"
CERTIFICATE_FILENAME = "harness-internal-ca.crt"


def register_harness_certificate_route(router: Router, *, certificate_path: Path) -> None:
    """Register the exact GET endpoint for the fixed Harness certificate."""

    def harness_certificate(handler: Any, params: Mapping[str, str]) -> None:
        if not certificate_path.is_file():
            return text_response(
                handler,
                HTTPStatus.NOT_FOUND,
                "Harness certificate is not available",
                "text/plain; charset=utf-8",
            )
        payload = certificate_path.read_bytes()
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "application/x-x509-ca-cert")
        handler.send_header(
            "Content-Disposition", f'attachment; filename="{CERTIFICATE_FILENAME}"'
        )
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(payload)

    router.get(CERTIFICATE_ROUTE, harness_certificate)
