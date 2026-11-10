"""Stream validated media into create-only files; no executable content or auth forwarding."""
import hashlib
import os
from pathlib import Path
import tempfile
from urllib.parse import urljoin

from PIL import Image

from xuloom.media.queue import client, download_url

from xuloom.features.media_files import IMAGE_LIMIT, MEDIA_LIMIT, _contains, _mp4_payload, file_sha256, identify  # noqa: F401


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
        return publish(temporary, directory, stem, kind, mime)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish(source, directory, stem, kind, mime=""):
    source, directory = Path(source), Path(directory).resolve()
    actual_mime, extension = identify(source, kind)
    size = source.stat().st_size
    if size < 16 or size > (IMAGE_LIMIT if kind in {"image", "image-edit"} else MEDIA_LIMIT):
        raise ValueError("Generated media has an invalid size")
    aliases = {"audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/mp3": "audio/mpeg"}
    if aliases.get(mime, mime) not in {"", "application/octet-stream", "binary/octet-stream", actual_mime}:
        raise ValueError("Response MIME type disagrees with its media bytes")
    directory.mkdir(parents=True, exist_ok=True)
    target, fingerprint = directory / (stem + extension), file_sha256(source)
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.stat().st_size != size or file_sha256(target) != fingerprint:
            raise ValueError("An existing output conflicts with this media task; no file was overwritten")
    else:
        # Copy into the destination volume, flush, then publish create-only.
        import shutil
        with tempfile.NamedTemporaryFile(prefix=".xuloom-media-", dir=directory, delete=False) as output:
            temporary = Path(output.name)
            try:
                with source.open("rb") as incoming: shutil.copyfileobj(incoming, output)
                output.flush(); os.fsync(output.fileno())
                os.link(temporary, target)
            finally:
                output.close(); temporary.unlink(missing_ok=True)
    return {"path": str(target), "mime": actual_mime, "size": size, "sha256": fingerprint}
