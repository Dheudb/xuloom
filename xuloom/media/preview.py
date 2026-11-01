"""Bounded media reads over the existing RPC, including SSH; never expose file URLs."""
import base64
import hashlib
import json
import os

from xuloom.features.file_changes import preview_path
from xuloom.media.files import identify, IMAGE_LIMIT, MEDIA_LIMIT


def version(path, stat):
    return hashlib.sha256(json.dumps([str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino]).encode()).hexdigest()


def info(cwd, requested, known):
    path = preview_path(cwd, requested, known)
    # On Windows a newly published hard link may have stale directory-entry
    # creation metadata. Use the same handle-based stat contract as range reads.
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > MEDIA_LIMIT:
            raise ValueError("媒体文件超过 256 MB 预览限制")
        mime, _ = identify(path)
        if mime.startswith("image/") and before.st_size > IMAGE_LIMIT:
            raise ValueError("图片超过 20 MB 预览限制")
        after = os.fstat(stream.fileno())
    if version(path, before) != version(path, after):
        raise ValueError("媒体文件正在变化，请刷新后重试")
    return {"path": str(path), "name": path.name, "size": after.st_size, "mime": mime,
        "kind": mime.partition("/")[0], "version": version(path, after)}


def read(cwd, requested, known, expected_version, offset=0, length=262144):
    path = preview_path(cwd, requested, known)
    if path.suffix.lower() not in {".mp4", ".webm", ".m4a", ".weba", ".mp3", ".wav", ".flac", ".ogg"}:
        raise ValueError("分段读取仅支持音视频文件")
    return read_range(path, expected_version, offset, length, MEDIA_LIMIT)


def read_range(path, expected_version, offset, length, limit):
    """Read a previously authorized path; callers retain file-type/root policy."""
    if type(offset) is not int or type(length) is not int or offset < 0 or not 1 <= length <= 524288:
        raise ValueError("无效文件读取范围，单次最多 512 KB")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > limit or expected_version != version(path, before):
            raise ValueError("文件版本已变化，请重新打开预览")
        if offset > before.st_size:
            raise ValueError("读取位置超过文件大小")
        stream.seek(offset); value = stream.read(min(length, before.st_size - offset))
        if expected_version != version(path, os.fstat(stream.fileno())):
            raise ValueError("文件读取期间发生变化")
    return {"data": base64.b64encode(value).decode("ascii"), "offset": offset, "nextOffset": offset + len(value),
        "size": before.st_size, "eof": offset + len(value) >= before.st_size, "version": expected_version}
