import os
import json
import time
import hashlib
from pathlib import Path
from xuloom.core.storage import session_directory, data_directory, IOPath
from xuloom.core.session_lock import SessionLock, SessionConflictError, session_identity

class SessionManager:
    """
    Infinite Context Engine Cache Layer. 
    Drops conversation trees into lightweight logs maintaining persistence after crash or terminal exit.
    """
    def __init__(self, session_dir=None, cwd=None):
        self.cwd = os.path.realpath(cwd or os.getcwd())
        self.session_dir = str(session_dir if session_dir is not None else session_directory(cwd))
        IOPath(self.session_dir).mkdir(parents=True, exist_ok=True)
        self.last_save_error = ""
        self.last_save_path = ""
        self.last_save_ok = True
        self._writers = {}
        self._versions = {}
        self.lock_directory = Path(self.session_dir) / ".locks" if session_dir is not None else data_directory() / "session-locks"
        self.central = session_dir is None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release_writer()

    def acquire_writer(self, session_id):
        session_id = session_identity(session_id)
        created = session_id not in self._writers
        if session_id not in self._writers:
            lock = SessionLock(self.lock_directory, session_id)
            lock.acquire()
            self._writers[session_id] = lock
        if self.central:
            try:
                from xuloom.core.session_mutation import recover_pending
                recover_pending(session_id, self._writers)
            except BaseException as error:
                if created:
                    self.release_writer(session_id)
                if isinstance(error, (OSError, ValueError, KeyError)):
                    raise SessionConflictError("Pending session storage operation could not finish; history was preserved. Resolve the storage error before executing: " + str(error)) from error
                raise

    def release_writer(self, session_id=None):
        for identity in list(self._writers):
            if session_id is None or identity == session_id:
                self._writers.pop(identity).close()

    def owns_writer(self, session_id):
        return session_id in self._writers

    @staticmethod
    def _payload_revision(payload, *, legacy=False):
        if legacy and isinstance(payload, list):
            return 0
        if (not isinstance(payload, dict) or not isinstance(payload.get("messages", []), list)
                or not isinstance(payload.get("metadata", {}), dict)):
            raise SessionConflictError("Invalid session checkpoint structure; existing history was preserved.")
        revision = payload.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise SessionConflictError("Invalid session revision: expected a non-negative integer; existing history was preserved.")
        return revision

    @classmethod
    def _decode(cls, data, *, child=False):
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeError) as error:
            raise SessionConflictError("Session checkpoint could not be decoded; existing history was preserved.") from error
        revision = cls._payload_revision(payload, legacy=not child)
        return payload, (revision, hashlib.sha256(data).hexdigest())

    @classmethod
    def _version(cls, path):
        try:
            raw = IOPath(path).read_bytes()
        except FileNotFoundError:
            return (0, None)
        return cls._decode(raw)[1]

    def _session_path(self, session_id):
        return os.path.join(self.session_dir, session_identity(session_id) + ".json")

    def _save(self, path, payload, owner):
        self._payload_revision(payload)
        owned = self.owns_writer(owner)
        self.acquire_writer(owner)
        try:
            current = self._version(path)
            expected = self._versions.get(path, (0, None))
            if current != expected:
                raise SessionConflictError("Session changed since it was loaded. Reload the latest state before executing or saving; the existing checkpoint was preserved.")
            payload["revision"] = current[0] + 1
            self._write_json_atomic(path, payload)
            self._versions[path] = self._version(path)
        except BaseException:
            if not owned:
                self.release_writer(owner)
            raise

    def check_current(self, session_id):
        path = self._session_path(session_id)
        self._check_current_path(path)

    def check_subagent_current(self, agent_id):
        self._check_current_path(self.get_subagent_transcript_path(agent_id))

    def _check_current_path(self, path):
        if self._version(path) != self._versions.get(path, (0, None)):
            raise SessionConflictError("Session changed since it was loaded. Reload before executing; the existing checkpoint was preserved.")

    def load_subagent_transcript(self, agent_id):
        path = self.get_subagent_transcript_path(agent_id)
        data = IOPath(path).read_bytes()
        payload, version = self._decode(data, child=True)
        self._versions[path] = version
        return payload

    def _make_json_safe(self, value):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, dict):
            return {
                str(key): self._make_json_safe(item)
                for key, item in value.items()
            }

        if isinstance(value, (list, tuple, set)):
            return [self._make_json_safe(item) for item in value]

        if hasattr(value, "model_dump") and callable(value.model_dump):
            return self._make_json_safe(value.model_dump())

        if hasattr(value, "dict") and callable(value.dict):
            return self._make_json_safe(value.dict())

        if hasattr(value, "to_dict") and callable(value.to_dict):
            return self._make_json_safe(value.to_dict())

        if hasattr(value, "__dict__"):
            return self._make_json_safe(vars(value))

        return repr(value)

    def _write_json_atomic(self, path: str, payload: dict):
        from xuloom.core.storage import atomic_json
        atomic_json(path, payload)
        
    def save_session_state(self, session_id: str, messages: list, metadata: dict = None):
        path = ""
        try:
            path = self._session_path(session_id)
            payload = {
                "messages": self._make_json_safe(messages),
                "metadata": self._make_json_safe(metadata or {}),
            }
            self._save(path, payload, session_id)
            self.last_save_ok = True
            self.last_save_error = ""
            self.last_save_path = path
            return True
        except Exception as e:
            self.last_save_ok = False
            self.last_save_error = str(e)
            self.last_save_path = path
            return False

    def save_session(self, session_id: str, messages: list):
        self.save_session_state(session_id, messages, metadata={})
            
    def load_session_state(self, session_id: str) -> dict:
        path = self._session_path(session_id)
        try:
            data = IOPath(path).read_bytes()
        except FileNotFoundError:
            return {"messages": [], "metadata": {}}
        payload, version = self._decode(data)
        self._versions[path] = version
        if isinstance(payload, list):
            return {"messages": payload, "metadata": {}, "_valid": True}
        return {"messages": payload.get("messages", []), "metadata": payload.get("metadata", {}), "_valid": True}

    def load_session(self, session_id: str) -> list:
        return self.load_session_state(session_id).get("messages", [])

    def save_subagent_transcript(self, agent_id: str, payload: dict):
        path = ""
        try:
            path = self.get_subagent_transcript_path(agent_id)
            owner = payload.get("session_id") or payload.get("metadata", {}).get("delegation", {}).get("owner_session_id") or agent_id
            self._save(path, self._make_json_safe(payload), owner)
            self.last_save_ok = True
            self.last_save_error = ""
            self.last_save_path = path
            return True
        except Exception as e:
            self.last_save_ok = False
            self.last_save_error = str(e)
            self.last_save_path = path
            return False

    def get_subagent_transcript_path(self, agent_id: str) -> str:
        agent_id = session_identity(agent_id)
        subagent_dir = os.path.join(self.session_dir, "subagents")
        IOPath(subagent_dir).mkdir(parents=True, exist_ok=True)
        return os.path.join(subagent_dir, f"{agent_id}.json")
            
    def get_recent_sessions(self) -> str:
        """Returns string representation of the last 5 sessions."""
        try:
            files = [f for f in os.listdir(IOPath(self.session_dir)) if f.endswith('.json')]
            if not files:
                return "No existing sessions found."
                
            sorted_files = sorted(
                files, 
                key=lambda x: IOPath(self.session_dir, x).stat().st_mtime,
                reverse=True
            )
            
            out = "[bold cyan]Active Sessions Cache[/bold cyan]\n"
            for f in sorted_files[:5]:
                sid = f.replace('.json', '')
                mtime = IOPath(self.session_dir, f).stat().st_mtime
                time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime))
                # Listing history must not replace the loaded execution snapshot's revision.
                state = json.loads(IOPath(self.session_dir, f).read_bytes())
                if not isinstance(state, dict):
                    state = {}
                metadata = state.get("metadata", {})
                mode = metadata.get("plan", {}).get("mode", "normal")
                todo_count = len(metadata.get("todos", []))
                subagent_count = len(metadata.get("subagents", []))
                sandbox = metadata.get("sandbox", {})
                sandbox_label = sandbox.get("branch", "none") if sandbox else "none"
                out += (
                    f"  - ID: [green]{sid}[/green] (Last Modified: {time_str})\n"
                    f"    mode={mode}, todos={todo_count}, subagents={subagent_count}, sandbox={sandbox_label}\n"
                )
            out += "\n[dim]To resume, type: /resume <ID>[/dim]"
            return out
        except Exception as e:
            return f"Error retrieving sessions: {e}"

    def delete_session(self, session_id: str) -> bool:
        session_id = session_identity(session_id)
        owned = self.owns_writer(session_id)
        self.acquire_writer(session_id)
        try:
            path = self._session_path(session_id)
            found = IOPath(path).exists()
            if self.central:
                from xuloom.core.session_mutation import mutate
                mutate({"action": "delete", "sessions": [{"project": self.cwd, "session_id": session_id}]}, held=self._writers)
            else:
                if found:
                    IOPath(path).unlink()
                self._cleanup_subagents_for_session(session_id)
            self._cleanup_plan_files_for_session(session_id)
            self._versions.pop(path, None)
            return found
        finally:
            if not owned:
                self.release_writer(session_id)

    def _cleanup_subagents_for_session(self, session_id: str):
        """Remove subagent transcripts that belong to the given session."""
        subagent_dir = os.path.join(self.session_dir, "subagents")
        if not IOPath(subagent_dir).is_dir():
            return
        for fname in os.listdir(IOPath(subagent_dir)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(subagent_dir, fname)
            try:
                with IOPath(fpath).open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get("session_id") == session_id:
                    IOPath(fpath).unlink()
            except Exception:
                pass

    def _cleanup_plan_files_for_session(self, session_id: str):
        """Remove plan files generated by the given session."""
        try:
            from xuloom.features.plans import get_plan_file_path, get_plans_directory
        except ImportError:
            return
        plan_path = get_plan_file_path(session_id)
        if os.path.exists(plan_path):
            os.unlink(plan_path)
        plans_dir = get_plans_directory()
        if not os.path.isdir(plans_dir):
            return
        from xuloom.features.plans import _make_plan_slug
        slug = _make_plan_slug(session_id)
        for fname in os.listdir(plans_dir):
            if fname.startswith(slug) and fname.endswith(".md"):
                try:
                    os.unlink(os.path.join(plans_dir, fname))
                except Exception:
                    pass

    def list_session_ids(self) -> list:
        try:
            files = [f for f in os.listdir(IOPath(self.session_dir)) if f.endswith('.json')]
            return [f.replace('.json', '') for f in sorted(
                files,
                key=lambda x: IOPath(self.session_dir, x).stat().st_mtime,
                reverse=True,
            )]
        except Exception:
            return []

    def clear_all_sessions(self) -> int:
        identities = self.list_session_ids()
        acquired = []
        try:
            # Preflight every writer before deleting anything. A busy session is explicit,
            # and a failed batch never retains leases acquired solely for deletion.
            for sid in identities:
                if not self.owns_writer(sid):
                    self.acquire_writer(sid)
                    acquired.append(sid)
            return sum(self.delete_session(sid) for sid in identities)
        finally:
            for sid in acquired:
                self.release_writer(sid)
