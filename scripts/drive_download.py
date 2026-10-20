"""Stream authenticated drive files as a ZIP without buffering the archive."""
import re
import zipfile

from lan_chat import FILE_COPY_CHUNK_BYTES, LanChatError


class _ZipSink:
    def __init__(self):
        self.position = 0
        self.chunks = []

    def write(self, data):
        self.position += len(data)
        self.chunks.append(data)
        return len(data)

    def tell(self):
        return self.position

    def flush(self):
        pass

    def drain(self):
        chunks, self.chunks = self.chunks, []
        return chunks


def archive_entries(store, token, file_ids):
    store.authenticate(token)
    if not isinstance(file_ids, list) or not 1 <= len(file_ids) <= 1000:
        raise LanChatError("请选择 1 至 1000 个文件下载")
    entries, used, seen = [], set(), set()
    for file_id in file_ids:
        if not isinstance(file_id, str) or not re.fullmatch(r"[0-9a-f]{32}", file_id):
            raise LanChatError("文件编号无效")
        if file_id in seen:
            continue
        seen.add(file_id)
        path, name, _, size = store.drive_info(token, file_id)
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(" .") or "file"
        if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I):
            name = "_" + name
        stem, dot, suffix = name.rpartition(".")
        stem, extension = (stem, dot + suffix) if stem else (name, "")
        candidate, index = name, 1
        while candidate.casefold() in used:
            index += 1
            candidate = f"{stem} ({index}){extension}"
        used.add(candidate.casefold())
        entries.append((path, candidate, size))
    return entries


def archive_chunks(entries):
    sink = _ZipSink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path, name, size in entries:
            info = zipfile.ZipInfo(name)
            info.file_size = size
            with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
                remaining = size
                while remaining:
                    chunk = source.read(min(FILE_COPY_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise OSError("文件内容不完整，请重新下载")
                    target.write(chunk)
                    remaining -= len(chunk)
                    yield from sink.drain()
            yield from sink.drain()
    yield from sink.drain()


def archive_response(handler, store, token, file_ids):
    # Validate the entire selection before sending headers or any file bytes.
    entries = archive_entries(store, token, file_ids)
    handler.send_response(200)
    handler.send_header("Content-Type", "application/zip")
    handler.send_header("Content-Disposition", 'attachment; filename="cloud-drive.zip"')
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True
    try:
        for chunk in archive_chunks(entries):
            handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError, OSError):
        # The response has already started; never append JSON to ZIP bytes.
        pass
