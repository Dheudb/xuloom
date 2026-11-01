"""Authorized HTML assets, shared by local and SSH preview clients."""
import os
from pathlib import Path, PurePosixPath

from xuloom.features.file_changes import preview_path
from xuloom.media.preview import read_range, version

LIMIT = 30 * 1024 * 1024
TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json", ".txt": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".wasm": "application/wasm",
}


def resolve(cwd, requested, known, asset=None):
    entry = preview_path(cwd, requested, known)
    if entry.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("请选择 HTML 文件")
    workspace = Path(cwd).resolve()
    assets = entry.is_relative_to(workspace)
    root = workspace if assets else entry.parent
    relative = entry.relative_to(root).as_posix() if asset is None else asset
    if not isinstance(relative, str) or not relative or any(c in relative for c in "\0\\:"):
        raise ValueError("网页资源路径无效")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or any(part.startswith(".") for part in parts.parts):
        raise ValueError("网页资源超出允许范围")
    file = (root / relative).resolve()
    if not file.is_relative_to(root) or (not assets and file != entry) or any(
            part.startswith(".") for part in file.relative_to(root).parts):
        raise ValueError("网页资源超出允许范围")
    if file.suffix.lower() not in TYPES or not file.is_file():
        raise ValueError("网页资源类型不支持或文件不存在")
    return entry, file, relative


def info(cwd, requested, known, asset=None):
    entry, file, relative = resolve(cwd, requested, known, asset)
    with file.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        if stat.st_size > LIMIT:
            raise ValueError("资源超过预览大小限制（30 MB）")
    return {"path": str(entry), "asset": relative, "size": stat.st_size,
        "mime": TYPES[file.suffix.lower()], "version": version(file, stat)}


def read(cwd, requested, known, asset, expected_version, offset=0, length=262144):
    _, file, _ = resolve(cwd, requested, known, asset)
    return read_range(file, expected_version, offset, length, LIMIT)
