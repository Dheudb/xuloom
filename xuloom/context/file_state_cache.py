import hashlib
import os
import io
import re
from typing import Dict, Optional


class FileStateCache:
    def __init__(self):
        self.entries: Dict[str, dict] = {}

    def _normalize_path(self, path: str) -> str:
        return os.path.abspath(path)

    def _range_key(self, start_line=None, end_line=None) -> str:
        return f"{start_line or 1}:{end_line or '*'}"

    def _safe_stat(self, abs_path: str):
        try:
            return os.stat(abs_path)
        except Exception:
            return None

    def _compute_sha256(self, abs_path: str) -> str:
        digest = hashlib.sha256()
        with open(abs_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()[:16]

    def snapshot(self, path: str) -> Optional[dict]:
        abs_path = self._normalize_path(path)
        if not os.path.exists(abs_path) or not os.path.isfile(abs_path):
            return None

        stat = self._safe_stat(abs_path)
        if stat is None:
            return None

        try:
            sha256 = self._compute_sha256(abs_path)
        except Exception:
            sha256 = ""

        return {
            "path": abs_path,
            "size": int(getattr(stat, "st_size", 0) or 0),
            "mtime": float(getattr(stat, "st_mtime", 0.0) or 0.0),
            "sha256": sha256,
        }

    def has_been_read(self, path: str) -> bool:
        abs_path = self._normalize_path(path)
        entry = self.entries.get(abs_path, {})
        return bool(entry.get("has_been_read"))

    def text_page(self,path,start,maximum):
        """One integrity scan and sparse seek; no whole-file text/list allocation."""
        path=self._normalize_path(path)
        signature=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        initial_path=os.stat(path)
        with open(path,'rb') as stream:
            initial=os.fstat(stream.fileno());digest=hashlib.sha256()
            if (initial.st_dev,initial.st_ino)!=(initial_path.st_dev,initial_path.st_ino):raise ValueError('File replaced before reading')
            anchors=[0];lines=position=0;pending=b'';last=b''
            for chunk in iter(lambda:stream.read(1024*1024),b''):
                digest.update(chunk);last=chunk[-1:]
                block=pending+chunk;base=position-len(pending);position+=len(chunk)
                pending=b'\r' if block.endswith(b'\r') else b''
                if pending:block=block[:-1]
                for match in re.finditer(br'\r\n|\r|\n',block):
                    lines+=1
                    if lines%512==0:anchors.append(base+match.end())
            if pending:
                lines+=1
                if lines%512==0:anchors.append(position)
            total=lines+int(position>0 and last not in (b'\r',b'\n'))
            first=max(1,total+start+1) if start<0 else start
            if first>total and not (first==1 and not total):raise ValueError('requested offset is beyond end of file')
            block=(first-1)//512
            stream.seek(anchors[min(block,len(anchors)-1)])
            text=io.TextIOWrapper(stream,encoding='utf-8',newline=None)
            selected=[]
            try:
                for _ in range((first-1)%512):
                    while True:
                        part=text.readline(131073)
                        if not part or part.endswith('\n'):break
                for _ in range(min(maximum,total-first+1)):
                    line=text.readline(131073)
                    if len(line)>131072:raise ValueError('Line exceeds inline limit; use char_offset/char_limit instead; no full-line read was recorded')
                    selected.append(line)
                # Windows path-stat and fd-stat may expose different ctime semantics.
                # Compare each against its own baseline, while binding the same inode.
                if signature(initial)!=signature(os.fstat(stream.fileno())) or signature(initial_path)!=signature(os.stat(path)):
                    raise ValueError('File changed during reading; no read receipt was recorded')
            finally:text.detach()
        snapshot={'path':path,'size':initial.st_size,'mtime':initial.st_mtime,'sha256':digest.hexdigest()[:16]}
        return snapshot,first,selected,first+len(selected)<=total

    def get_entry(self, path: str) -> Optional[dict]:
        return self.entries.get(self._normalize_path(path))

    def invalidate(self, path):
        identity = os.path.normcase(os.path.realpath(path))
        for key in list(self.entries):
            if os.path.normcase(os.path.realpath(key)) == identity:
                self.entries.pop(key, None)

    def should_skip_redundant_read(self, path: str, *, start_line=None, end_line=None) -> bool:
        abs_path = self._normalize_path(path)
        entry = self.entries.get(abs_path)
        if not entry:
            return False

        current_snapshot = self.snapshot(abs_path)
        if not current_snapshot:
            return False

        if not current_snapshot.get("sha256") or current_snapshot.get("sha256") != entry.get("sha256"):
            return False

        range_key = self._range_key(start_line, end_line)
        ranges = entry.get("ranges", {})
        return range_key in ranges

    def record_read(
        self,
        path: str,
        *,
        kind: str,
        start_line=None,
        end_line=None,
        chars: int = 0,
        _snapshot: Optional[dict] = None,
    ) -> Optional[dict]:
        abs_path = self._normalize_path(path)
        snapshot = _snapshot if _snapshot is not None else self.snapshot(abs_path)
        if not snapshot:
            self.entries.pop(abs_path, None)
            return None

        range_key = self._range_key(start_line, end_line)
        previous = self.entries.get(abs_path, {})
        entry = {
            **snapshot,
            "kind": kind,
            "has_been_read": True,
            "last_read_range": range_key,
            "last_read_chars": int(chars or 0),
            "ranges": dict(previous.get("ranges", {})) if snapshot.get("sha256") and previous.get("sha256") == snapshot["sha256"] else {},
        }
        entry["ranges"][range_key] = {
            "kind": kind,
            "chars": int(chars or 0),
        }
        self.entries[abs_path] = entry
        return entry

    def record_write(self, path: str) -> Optional[dict]:
        abs_path = self._normalize_path(path)
        snapshot = self.snapshot(abs_path)
        if not snapshot:
            self.entries.pop(abs_path, None)
            return None

        previous = self.entries.get(abs_path, {})
        previous_ranges = dict(previous.get("ranges", {})) if snapshot.get("sha256") and previous.get("sha256") == snapshot["sha256"] else {}
        entry = {
            **snapshot,
            "kind": self.entries.get(abs_path, {}).get("kind", "text"),
            "has_been_read": True,
            "last_read_range": self.entries.get(abs_path, {}).get("last_read_range", "1:*"),
            "last_read_chars": self.entries.get(abs_path, {}).get("last_read_chars", 0),
            "ranges": previous_ranges,
            "last_write": {
                "size": snapshot.get("size", 0),
                "mtime": snapshot.get("mtime", 0.0),
                "sha256": snapshot.get("sha256", ""),
            },
        }
        self.entries[abs_path] = entry
        return entry

    def export_state(self) -> dict:
        return {
            "entries": self.entries,
        }

    def load_state(self, payload):
        payload = payload or {}
        entries = payload.get("entries", {})
        if isinstance(entries, dict):
            self.entries = entries
        else:
            self.entries = {}

    def render_summary(self) -> str:
        if not self.entries:
            return "File state cache: empty"

        lines = [f"File state cache ({len(self.entries)})"]
        for path, entry in list(self.entries.items())[:8]:
            lines.append(
                f"- {path} size={entry.get('size', 0)} sha={entry.get('sha256', '')} range={entry.get('last_read_range', '1:*')}"
            )
        return "\n".join(lines)
