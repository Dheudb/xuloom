"""Stream validated media into create-only files; no executable content or auth forwarding."""
import hashlib
import os
from pathlib import Path
import tempfile
from urllib.parse import urljoin

from PIL import Image

from xuloom.media.queue import client, download_url

IMAGE_LIMIT = 20 * 1024 * 1024
MEDIA_LIMIT = 256 * 1024 * 1024


def _contains(path, marker):
    with Path(path).open("rb") as stream:
        tail = b""
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return False
            combined = tail + chunk
            if marker in combined:
                return True
            tail = combined[-len(marker):]


def _mp4_payload(path):
    size, position, movie, data = Path(path).stat().st_size, 0, False, False
    with Path(path).open("rb") as stream:
        for _ in range(10000):
            if position + 8 > size:
                break
            stream.seek(position); header = stream.read(16)
            length = int.from_bytes(header[:4], "big"); prefix = 8
            if length == 1:
                length = int.from_bytes(header[8:16], "big"); prefix = 16
            elif length == 0:
                length = size - position
            if length < prefix or position + length > size:
                return False
            movie = movie or header[4:8] == b"moov"
            data = data or header[4:8] == b"mdat" and length > prefix
            position += length
            if movie and data:
                return True
    return False


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def identify(path, kind=None):
    path = Path(path)
    with path.open("rb") as stream:
        header = stream.read(32)
    if header.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM")) or header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        with Image.open(path) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError("Generated image exceeds 40 million pixels")
            form = image.format
            image.verify()
        formats = {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"),
            "WEBP": ("image/webp", ".webp"), "GIF": ("image/gif", ".gif"), "BMP": ("image/bmp", ".bmp")}
        result = formats.get(form)
    elif header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        result = "audio/wav", ".wav"
    elif header.startswith(b"fLaC"):
        result = "audio/flac", ".flac"
    elif header.startswith(b"OggS"):
        result = "audio/ogg", ".ogg"
    elif header.startswith(b"ID3") or len(header) >= 2 and header[0] == 255 and header[1] & 224 == 224 and header[1] & 6:
        result = "audio/mpeg", ".mp3"
    elif header[4:8] == b"ftyp":
        if not _mp4_payload(path):
            raise ValueError("MP4 container has no complete movie/media payload")
        result = ("audio/mp4", ".m4a") if kind in {"speech", "sound", "music"} or path.suffix.lower() == ".m4a" else ("video/mp4", ".mp4")
    elif header.startswith(b"\x1aE\xdf\xa3"):
        if not _contains(path, b"webm") or not _contains(path, b"\x1fC\xb6u"):
            raise ValueError("WebM container has no media cluster")
        result = ("audio/webm", ".weba") if kind in {"speech", "sound", "music"} or path.suffix.lower() == ".weba" else ("video/webm", ".webm")
    else:
        result = None
    if not result:
        raise ValueError("Unsupported or invalid generated media format")
    family = "image/" if kind in {"image", "image-edit"} else "video/" if kind == "video" else "audio/" if kind else None
    if family and not result[0].startswith(family):
        raise ValueError("Downloaded file type does not match the requested media kind")
    return result


async def download(binding, descriptor, directory, stem):
    kind = binding["model"]["kind"]
    maximum = IMAGE_LIMIT if kind in {"image", "image-edit"} else MEDIA_LIMIT
    if descriptor.get("size") and descriptor["size"] > maximum:
        raise ValueError("Generated file exceeds the download limit")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        url = download_url(binding, descriptor["url"])
        async with client(binding) as transport:
            for _ in range(4):
                async with transport.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        url = download_url(binding, urljoin(url, response.headers.get("location", "")))
                        continue
                    response.raise_for_status()
                    length = response.headers.get("content-length")
                    if length is not None and (not length.isdecimal() or int(length) > maximum):
                        raise ValueError("Invalid or oversized media response")
                    mime = response.headers.get("content-type", "").partition(";")[0].lower().strip()
                    digest, size = hashlib.sha256(), 0
                    with tempfile.NamedTemporaryFile(prefix=".xuloom-media-", dir=directory, delete=False) as output:
                        temporary = Path(output.name)
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > maximum:
                                raise ValueError("Generated file exceeds the download limit")
                            output.write(chunk); digest.update(chunk)
                        output.flush(); os.fsync(output.fileno())
                    break
            else:
                raise ValueError("Too many media download redirects")
        if size < 16 or descriptor.get("size") is not None and size != descriptor["size"]:
            raise ValueError("Generated media is empty or its declared size does not match")
        actual_mime, extension = identify(temporary, kind)
        aliases = {"audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/mp3": "audio/mpeg"}
        if aliases.get(mime, mime) not in {"", "application/octet-stream", "binary/octet-stream", actual_mime}:
            raise ValueError("Response MIME type disagrees with its media bytes")
        target = directory / (stem + extension)
        fingerprint = digest.hexdigest()
        if target.exists():
            if target.is_symlink() or not target.is_file() or target.stat().st_size != size:
                raise ValueError("An existing output conflicts with this media task; no file was overwritten")
            if file_sha256(target) != fingerprint:
                raise ValueError("An existing output changed; no file was overwritten")
        else:
            # The complete, flushed sibling is published without replacing a racing file.
            os.link(temporary, target)
        return {"path": str(target), "mime": actual_mime, "size": size, "sha256": fingerprint}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
