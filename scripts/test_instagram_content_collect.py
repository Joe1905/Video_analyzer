#!/usr/bin/env python3
"""Focused offline checks for Instagram content collection helpers."""

import sqlite3
import tempfile
from pathlib import Path

from instagram_content_collect import _safe_error, canonical_url, instagram_login_status, media_id_from_url


def test_canonical_url_removes_token() -> None:
    value = "https://www.instagram.com/accounts/insights/content/?token=secret#section"
    assert canonical_url(value) == "https://www.instagram.com/accounts/insights/content/"


def test_media_id_uses_canonical_path() -> None:
    assert media_id_from_url("https://www.instagram.com/reel/ABC123/?token=secret") == "ABC123"
    assert media_id_from_url("https://www.instagram.com/explore/") == ""


def test_error_redacts_query_token() -> None:
    text = _safe_error("page failed at https://www.instagram.com/reel/ABC123/?token=secret")
    assert "token=secret" not in text
    assert text.endswith("https://www.instagram.com/reel/ABC123/")


def test_login_status_never_returns_cookie_values() -> None:
    # Some Windows endpoint scanners retain a just-created file named Cookies.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        cookie_path = Path(temporary) / "Default" / "Cookies"
        cookie_path.parent.mkdir(parents=True)
        with sqlite3.connect(cookie_path) as conn:
            conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
            conn.execute("INSERT INTO cookies VALUES (?, ?, ?)", (".instagram.com", "sessionid", "must-not-leak"))
            conn.commit()
        status = instagram_login_status(Path(temporary))
    assert status["profile_has_instagram_login"] is True
    assert status["cookie_names"] == ["sessionid"]
    assert "must-not-leak" not in str(status)


if __name__ == "__main__":
    test_canonical_url_removes_token()
    test_media_id_uses_canonical_path()
    test_error_redacts_query_token()
    test_login_status_never_returns_cookie_values()
    print("instagram content collect helper tests passed")
