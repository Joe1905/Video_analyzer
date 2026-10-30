"""Bounded document storage shared by chat uploads and generated downloads."""
import base64
import binascii
import io
import json
from pathlib import Path
import re
import uuid
import zipfile

FILE_TYPES = {
    '.pdf': 'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.csv': 'text/csv', '.txt': 'text/plain', '.md': 'text/markdown',
}
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 30 * 1024 * 1024


def clean_filename(name):
    name = str(name or 'file').replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f]', '', name)
    if not name or len(name) > 160:
        raise ValueError('文件名为空或过长')
    return name


def validate_document(name, data):
    suffix = Path(name).suffix.lower()
    if suffix not in FILE_TYPES:
        raise ValueError('支持 PDF、DOCX、XLSX、CSV、TXT、MD 文件')
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError('文件不能为空，单个文件最大 10MB')
    if suffix == '.pdf' and not data.startswith(b'%PDF-'):
        raise ValueError('PDF 文件格式无效')
    if suffix in {'.docx', '.xlsx'}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                required = 'word/document.xml' if suffix == '.docx' else 'xl/workbook.xml'
                if required not in archive.namelist() or sum(i.file_size for i in archive.infolist()) > 100 * 1024 * 1024:
                    raise ValueError('Office 文件格式无效或解压后过大')
        except zipfile.BadZipFile:
            raise ValueError('Office 文件格式无效') from None
    return suffix


def decode_document(item):
    name = clean_filename(item.get('name'))
    encoded = str(item.get('dataUrl') or '')
    if len(encoded) > MAX_FILE_BYTES * 4 // 3 + 1024:
        raise ValueError('单个文件最大 10MB')
    match = re.fullmatch(r'data:[^;,]*;base64,(.*)', encoded, re.DOTALL)
    if not match:
        raise ValueError('文件数据无效')
    try:
        data = base64.b64decode(match.group(1), validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('文件编码无效') from None
    suffix = validate_document(name, data)
    return name, FILE_TYPES[suffix], data


def save_document(directory, name, data, generated=False):
    name = clean_filename(name)
    suffix = validate_document(name, data)
    directory.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex
    meta = {'id': file_id, 'name': name, 'type': FILE_TYPES[suffix], 'size': len(data),
            'url': '/api/chat/attachments/' + file_id, 'generated': generated, 'kind': 'file'}
    (directory / (file_id + suffix)).write_bytes(data)
    (directory / (file_id + '.meta.json')).write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    return meta
