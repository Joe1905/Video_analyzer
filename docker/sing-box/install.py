#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: install.py VERSION SHA256 OUTPUT")
    version, expected_sha256, output_value = sys.argv[1:]
    archive_name = f"sing-box-{version}-linux-amd64-glibc.tar.gz"
    url = f"https://github.com/SagerNet/sing-box/releases/download/v{version}/{archive_name}"
    output = Path(output_value)
    with tempfile.TemporaryDirectory(prefix="sing-box-download-") as temporary_value:
        temporary = Path(temporary_value)
        archive = temporary / archive_name
        urllib.request.urlretrieve(url, archive)
        actual_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual_sha256 != expected_sha256:
            raise RuntimeError(
                f"sing-box archive checksum mismatch: expected {expected_sha256}, got {actual_sha256}"
            )
        with tarfile.open(archive, "r:gz") as bundle:
            member_name = f"sing-box-{version}-linux-amd64-glibc/sing-box"
            member = bundle.getmember(member_name)
            if not member.isfile():
                raise RuntimeError(f"sing-box executable missing from {archive_name}")
            source = bundle.extractfile(member)
            if source is None:
                raise RuntimeError(f"unable to extract sing-box executable from {archive_name}")
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("wb") as destination:
                shutil.copyfileobj(source, destination)
    output.chmod(0o755)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
