"""Immutable image snapshots for local tool receipts, separate from model input."""
import base64
import hashlib
import io
from pathlib import Path
import re

from PIL import Image
from xuloom.core.storage import atomic_bytes

MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 40_000_000
FORMATS = {"PNG", "JPEG", "WEBP", "GIF", "BMP"}


def artifact_owner(context):
    delegation = context.get("delegation") or {}
    identity = delegation.get("root_session_id") or delegation.get("owner_session_id") or context.get("session_id")
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
        raise ValueError("Invalid media owner session")
    return identity


def archive_images(content, directory=None, name=""):
    media = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "image":
            continue
        source = block.get("source", {})
        try:
            if source.get("type") != "base64":
                raise ValueError("非内嵌图像，未自动下载外部地址")
            encoded = source.get("data", "")
            if not isinstance(encoded, str) or len(encoded) > MAX_BYTES * 4 // 3 + 8:
                raise ValueError("图像超过预览大小限制")
            data = base64.b64decode(encoded, validate=True)
            if len(data) > MAX_BYTES:
                raise ValueError("图像超过预览大小限制")
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in FORMATS or image.width * image.height > MAX_PIXELS:
                    raise ValueError("图像格式或尺寸不支持预览")
                width, height = image.size
            identity = hashlib.sha256(data).hexdigest()
            if directory is not None:
                root = Path(directory) / "images"
                for candidate in (Path(directory), root, root / (identity + ".image")):
                    if candidate.is_symlink() or candidate.exists() and getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400:
                        raise ValueError("拒绝写入链接图像")
                root.mkdir(parents=True, exist_ok=True)
                atomic_bytes(root / (identity + ".image"), data)
            media.append({"id": identity, "kind": "image", "name": str(name or f"图片 {len(media) + 1}"), "width": width, "height": height})
        except Exception as error:
            media.append({"kind": "image", "name": str(name or "图片"), "error": "图片预览不可用：" + str(error)[:180]})
    return media


def image_preview(directory, identity, *, thumbnail=False):
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise ValueError("无效的图像引用")
    for candidate in (Path(directory), Path(directory) / "images"):
        if candidate.is_symlink() or candidate.exists() and getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("拒绝通过链接读取图像")
    root = (Path(directory) / "images").resolve()
    path = (root / (identity + ".image")).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("此会话的图像不存在或已被删除")
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != identity:
        raise ValueError("图像存档已变化，不能作为原工具结果显示")
    return preview_image_bytes(data, thumbnail=thumbnail)


def preview_image_bytes(data, *, thumbnail=False):
    if len(data) > MAX_BYTES:
        raise ValueError("图像超过预览大小限制")
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in FORMATS or image.width * image.height > MAX_PIXELS:
            raise ValueError("图像格式或尺寸不支持预览")
        width, height = image.size
        image.seek(0)
        image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
        edge = 256 if thumbnail else 2400
        image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="PNG")
    return {"source": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii"),
        "width": width, "height": height, "previewWidth": image.width, "previewHeight": image.height}


def receipt_text(content):
    if not isinstance(content, list):
        return str(content or "")
    return "\n".join(str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text")
