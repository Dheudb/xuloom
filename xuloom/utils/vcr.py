"""Legacy request-hash recordings (``XULOOM_VCR_MODE``).

Superseded by :class:`xuloom.testing.ReplayModel`: a hash only matches a byte-identical
request, and the system prompt differs between sessions, so these recordings rarely replay
outside the session that made them. Kept working for existing users; not developed further.
"""
import copy
import hashlib
import json
import logging
import os
import time

_legacy_warned = False


def _warn_legacy(mode: str) -> None:
    global _legacy_warned
    if not _legacy_warned:
        _legacy_warned = True
        logging.getLogger("xuloom.vcr").warning(
            "XULOOM_VCR_MODE=%s uses legacy request-hash recordings, which rarely match across sessions. "
            "Prefer EngineConfig(record_responses=...) with xuloom.testing.ReplayModel (docs/replay-and-eval.md).", mode)


class VCRReplayResponse:
    def __init__(self, payload):
        payload = payload or {}
        self.stop_reason = payload.get("stop_reason")
        self.content = [VCRReplayBlock(item) for item in payload.get("content", []) or []]
        self.usage = VCRReplayUsage(payload.get("usage", {}) or {})


class VCRReplayUsage:
    def __init__(self, payload):
        payload = payload or {}
        for key, value in payload.items():
            setattr(self, key, value)


class VCRReplayBlock:
    def __init__(self, payload):
        payload = payload or {}
        for key, value in payload.items():
            setattr(self, key, value)
        if not hasattr(self, "type"):
            self.type = "text"
        if self.type == "text" and not hasattr(self, "text"):
            self.text = ""
        if self.type == "tool_use":
            if not hasattr(self, "id"):
                self.id = ""
            if not hasattr(self, "name"):
                self.name = ""
            if not hasattr(self, "input"):
                self.input = {}


class ReplayMissError(RuntimeError):
    """Strict replay found no usable recording; no live request was made."""


class VCRManager:
    def __init__(self, cassette_dir: str = ".xuloom/vcr", mode: str = None):
        self.cassette_dir = cassette_dir
        # An explicit mode (argument or XULOOM_VCR_MODE) is a run setting: saved sessions never override it.
        self.explicit_mode = bool(mode or os.environ.get("XULOOM_VCR_MODE"))
        self.mode = str(mode or os.environ.get("XULOOM_VCR_MODE", "off") or "off").lower()
        self.history = []
        if self.explicit_mode and self.mode != "off":
            _warn_legacy(self.mode)

    def export_state(self) -> dict:
        return {
            "cassette_dir": self.cassette_dir,
            "mode": self.mode,
            "history": copy.deepcopy(self.history),
        }

    def load_state(self, payload):
        payload = payload or {}
        cassette_dir = payload.get("cassette_dir")
        if cassette_dir:
            self.cassette_dir = cassette_dir
        if not self.explicit_mode:
            self.mode = str(payload.get("mode", self.mode or "off") or "off").lower()
        self.history = list(payload.get("history", []) or [])[:40]

    def _hash_request(self, request_payload: dict) -> str:
        raw = json.dumps(request_payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _cassette_path(self, request_hash: str) -> str:
        return os.path.abspath(os.path.join(self.cassette_dir, f"{request_hash}.json"))

    def build_request_payload(self, *, model: str, system: str, messages: list, tools: list, max_tokens: int, thinking: dict = None, provider: str = "anthropic", reasoning: dict = None) -> dict:
        result = {
            "provider": provider,
            "model": model,
            "system": system,
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(tools),
            "max_tokens": int(max_tokens),
            "thinking": copy.deepcopy(thinking),
        }
        if reasoning is not None:
            result["reasoning"] = copy.deepcopy(reasoning)
        return result

    def try_replay(self, request_payload: dict):
        if self.mode != "replay":
            return None
        request_hash = self._hash_request(request_payload)
        path = self._cassette_path(request_hash)
        # Strict replay never falls back to a live (billed) request.
        if not os.path.exists(path):
            self._record_history("replay_miss", request_hash, path)
            raise ReplayMissError(f"XULOOM_VCR_MODE=replay: no recording for request {request_hash} in {self.cassette_dir}; "
                                  "record it with XULOOM_VCR_MODE=replay_record or switch replay off")
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError) as error:
            raise ReplayMissError(f"Recording {path} cannot be read: {error}") from error
        self._record_history("replay_hit", request_hash, path)
        return VCRReplayResponse(payload.get("response", {}))

    def record(self, request_payload: dict, response_payload: dict):
        if self.mode not in {"record", "replay_record"}:
            return None
        request_hash = self._hash_request(request_payload)
        path = self._cassette_path(request_hash)
        try:
            os.makedirs(self.cassette_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "request_hash": request_hash,
                        "recorded_at": time.time(),
                        "request": request_payload,
                        "response": response_payload,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            self._record_history("record", request_hash, path)
            return path
        except Exception:
            return None

    # Auxiliary (compaction/memory) requests share the cassette directory, keyed by their own payload.
    def auxiliary_payload(self, *, model: str, purpose: str, messages) -> dict:
        return {"auxiliary": purpose, "model": model, "messages": copy.deepcopy(messages or [])}

    def replay_auxiliary(self, request_payload: dict):
        """``(text, usage)`` for a recorded auxiliary request; strict replay never falls back to a live call."""
        if self.mode != "replay":
            return None
        request_hash = self._hash_request(request_payload)
        path = self._cassette_path(request_hash)
        if not os.path.exists(path):
            self._record_history("replay_miss", request_hash, path)
            raise ReplayMissError(f"XULOOM_VCR_MODE=replay: no recording for {request_payload.get('auxiliary')} request "
                                  f"{request_hash} in {self.cassette_dir}; no live request was made")
        try:
            with open(path, "r", encoding="utf-8") as f:
                response = json.load(f).get("response") or {}
        except (OSError, ValueError) as error:
            raise ReplayMissError(f"Recording {path} cannot be read: {error}") from error
        if not isinstance(response.get("text"), str):
            raise ReplayMissError(f"Recording {path} is not an auxiliary response")
        self._record_history("replay_hit", request_hash, path)
        return response["text"], dict(response.get("usage") or {})

    def record_auxiliary(self, request_payload: dict, text: str, usage: dict):
        return self.record(request_payload, {"text": text, "usage": dict(usage or {})})

    def _record_history(self, event: str, request_hash: str, path: str):
        self.history.insert(0, {
            "event": event,
            "request_hash": request_hash,
            "path": path,
            "timestamp": time.time(),
        })
        self.history = self.history[:40]
