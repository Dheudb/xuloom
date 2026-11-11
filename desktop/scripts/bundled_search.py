"""Stage the pinned upstream Windows search binary and its licenses at build time."""
import hashlib
from pathlib import Path
import urllib.request
import zipfile

VERSION = "15.2.0"
ARCHIVE_NAME = f"ripgrep-{VERSION}-x86_64-pc-windows-msvc.zip"
ARCHIVE_SHA256 = "71b2fef860abe467217a538ff31de02f5258807c0129f771846f87bd029aafc5"
BINARY_SHA256 = "14231169855ec5205cf5a1b6f1db358ff4aed4247c86b69ce8aae647c77f6680"
URL = f"https://github.com/BurntSushi/ripgrep/releases/download/{VERSION}/{ARCHIVE_NAME}"


def prepare_search(destination, *, cache=None):
    """No runtime download: verify the release archive before installing any files."""
    cache = Path(cache) if cache is not None else Path(__file__).resolve().parents[1] / "build/downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / ARCHIVE_NAME
    if not archive.exists():
        with urllib.request.urlopen(URL, timeout=60) as response:
            raw = response.read()
        if hashlib.sha256(raw).hexdigest() != ARCHIVE_SHA256:
            raise ValueError("ripgrep release checksum mismatch")
        archive.write_bytes(raw)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("Cached ripgrep release checksum mismatch")
    target = Path(destination) / "xuloom_search"
    with zipfile.ZipFile(archive) as source:
        files = {name: source.read(ARCHIVE_NAME[:-4] + "/" + name)
                 for name in ("rg.exe", "COPYING", "LICENSE-MIT", "UNLICENSE")}
    target.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (target / name).write_bytes(content)
    return target


def validate_search_executables(stage):
    """Keep the resource-only updater's guard; allow only the pinned search tool."""
    root = Path(stage)
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".exe":
            continue
        if path.relative_to(root).as_posix() != "dependencies/xuloom_search/rg.exe":
            raise ValueError("Unexpected executable in resource update")
        if hashlib.sha256(path.read_bytes()).hexdigest() != BINARY_SHA256:
            raise ValueError("Bundled ripgrep binary checksum mismatch")
