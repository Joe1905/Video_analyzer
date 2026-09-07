"""Only reuse successful visual analysis; retain rejected artifacts for diagnosis."""
import json
import uuid
from pathlib import Path
from viral_elements import require_visual_evidence, ViralElementError

ANALYSIS_FILES = ('analysis_zh.json', 'analysis.json', 'direct_analysis_zh.json', 'direct_analysis.json')

def valid_analysis(path):
    try:
        data = Path(path).read_text(encoding='utf-8')
        source = json.loads(data)
        if not isinstance(source, dict):
            return None
        require_visual_evidence(source)
        return source
    except (OSError, ValueError, TypeError, ViralElementError):
        return None

def best_analysis(directory):
    paths = [Path(directory) / name for name in ANALYSIS_FILES]
    for path in sorted((p for p in paths if p.is_file()), key=lambda p:p.stat().st_mtime, reverse=True):
        source = valid_analysis(path)
        if source:
            return {**source, 'source_channel':'A' if path.name.startswith('direct_') else 'B'}
    return None

def archive_invalid_analysis(directory):
    directory = Path(directory)
    invalid = [directory / name for name in ANALYSIS_FILES if (directory / name).is_file() and not valid_analysis(directory / name)]
    if not invalid:
        return []
    archive = directory / 'failed_visual_archive' / uuid.uuid4().hex
    archive.mkdir(parents=True)
    for path in invalid:
        path.rename(archive / path.name)
    return [p.name for p in invalid]
