"""Remove only an explicitly owned temporary fixture, including Windows long paths."""
import os
from pathlib import Path
import shutil
import stat
import tempfile

from xuloom.core.storage import IOPath


def remove_fixture(root, prefix):
    root = Path(root).resolve()
    if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith(prefix):
        raise ValueError('Refusing to remove a directory outside the owned fixture')
    def writable(operation, path, detail):
        if not isinstance(detail[1], PermissionError):
            raise detail[1]
        if not IOPath(path).resolve().is_relative_to(IOPath(root).resolve()):
            raise ValueError('Fixture cleanup escaped its owner')
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        operation(path)
    shutil.rmtree(IOPath(root), onerror=writable)
