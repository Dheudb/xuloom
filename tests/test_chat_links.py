import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from xuloom.core.storage import atomic_json, history_root
from xuloom.features.chat_links import ChatLinkError, chat_link, remote_chat_link, parse_link, resolve_link
from xuloom.remote.config import validate_profile, profile_key


class ChatLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xuloom links ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(self.root / "data")})
        self.env.start(); self.addCleanup(self.env.stop)
        self.project = self.root / "项目 with spaces"; self.project.mkdir()

    def record(self, project, sid="chat-1", metadata=None, **item):
        root = history_root(project)
        atomic_json(root / "project.json", {"version": 1, "project": str(project.resolve())})
        atomic_json(root / "sessions" / (sid + ".json"), {"messages": [{"role": "user", "content": "PRIVATE CHAT"}], "metadata": metadata or {}})
        atomic_json(root / "index.json", {"version": 1, "sessions": {sid: item}})
        return root / "sessions" / (sid + ".json")

    def test_link_is_path_free_strict_and_read_only(self):
        checkpoint = self.record(self.project)
        before = checkpoint.read_bytes()
        url = chat_link(self.project, "chat-1")
        self.assertNotIn(str(self.project), url)
        self.assertEqual(parse_link(url), (history_root(self.project).name, "chat-1"))
        row = resolve_link(url, require_openable=True)
        self.assertEqual(row["project"], str(self.project.resolve()))
        self.assertNotIn("PRIVATE CHAT", json.dumps(row))
        self.assertEqual(checkpoint.read_bytes(), before)
        for bad in (url + "\n", url + "?prompt=run", url + "#fragment", url + "/", url.replace("chat/v1", "chat/v2"),
                    url.replace("chat/v1", "remote/v1"), url.replace("chat-1", ".."), url.replace("chat-1", "%2e%2e"),
                    url.replace("chat/v1", "user:pass@chat/v1"), "file:///etc/passwd", None, url + "x" * 512):
            with self.subTest(bad=bad), self.assertRaises(ChatLinkError):
                resolve_link(bad)

    def test_moves_follow_recorded_provenance_never_uuid_guesses_or_forks(self):
        old = self.record(self.project)
        url = chat_link(self.project, "chat-1")
        other = self.root / "other"; other.mkdir()
        self.record(other)
        old.unlink()
        with self.assertRaises(ChatLinkError) as missing: resolve_link(url)
        self.assertEqual(missing.exception.code, "not_found")
        self.record(other, metadata={"workspace_moves": [{"from": str(self.project.resolve()), "to": str(other.resolve())}]})
        row = resolve_link(url, require_openable=True)
        self.assertTrue(row["moved"]); self.assertEqual(row["project"], str(other.resolve()))
        self.assertNotEqual(row["id"].split(":")[0], parse_link(url)[0])

    def test_archive_missing_directory_and_corruption_are_not_fresh_chats(self):
        checkpoint = self.record(self.project, archived=True)
        url = chat_link(self.project, "chat-1")
        self.assertTrue(resolve_link(url)["archived"])
        with self.assertRaises(ChatLinkError) as archived: resolve_link(url, require_openable=True)
        self.assertEqual(archived.exception.code, "archived")
        self.record(self.project)
        atomic_json(self.root / "data/workbench.json", {"projectMeta": {os.path.normcase(str(self.project.resolve())): {"archived": True}}})
        with self.assertRaises(ChatLinkError) as archived: resolve_link(url, require_openable=True)
        self.assertEqual(archived.exception.code, "archived")
        (self.root / "data/workbench.json").unlink()
        self.project.rmdir()
        self.assertFalse(resolve_link(url)["workspace_exists"])
        with self.assertRaises(ChatLinkError) as missing: resolve_link(url, require_openable=True)
        self.assertEqual(missing.exception.code, "workspace_missing"); self.assertFalse(self.project.exists())
        checkpoint.write_text("bad json")
        with self.assertRaises(ChatLinkError) as broken: resolve_link(url)
        self.assertEqual(broken.exception.code, "history_unreadable")

    def test_misbound_manifest_and_unreadable_file_fail_explicitly(self):
        checkpoint = self.record(self.project)
        url = chat_link(self.project, "chat-1")
        with patch.object(Path, "read_bytes", side_effect=PermissionError("PRIVATE SECRET")):
            with self.assertRaises(ChatLinkError) as unreadable: resolve_link(url)
        self.assertNotIn("PRIVATE SECRET", str(unreadable.exception))
        atomic_json(checkpoint.parent.parent / "project.json", {"version": 1, "project": str(self.root)})
        with self.assertRaises(ChatLinkError): resolve_link(url)

    def test_actual_cli_generates_and_resolves_without_model_or_new_history(self):
        self.record(self.project)
        url = chat_link(self.project, "chat-1")
        paths = sorted(str(p) for p in (self.root / "data").rglob("*"))
        # The CLI must choose UTF-8 even when Python initially uses an ASCII
        # stream; the caller explicitly decodes the documented wire format.
        env = {**os.environ, "PYTHONIOENCODING": "ascii"}
        for flags in (["--cwd", str(self.project), "--session", "chat-1"], ["--resolve", url]):
            process = subprocess.run([sys.executable, "-m", "xuloom.main", "chat-link", *flags, "--json"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)["result"]
            self.assertEqual(result["url"], url); self.assertTrue(result["ok"])
        bad = subprocess.run([sys.executable, "-m", "xuloom.main", "chat-link", "--resolve", url + "?run=bad", "--json"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
        self.assertEqual(bad.returncode, 1)
        self.assertEqual(json.loads(bad.stdout)["result"]["reason"], "invalid_link")
        self.assertEqual(paths, sorted(str(p) for p in (self.root / "data").rglob("*")))

    def test_cli_chat_opens_resolved_workspace_with_strict_resume(self):
        from xuloom.main import main
        self.record(self.project)
        url = chat_link(self.project, "chat-1")
        with patch.object(sys, "argv", ["xuloom", "--chat", url]), patch("xuloom.main.interactive_loop", new_callable=AsyncMock) as run:
            main()
        self.assertEqual(run.call_args.kwargs["resume_session_id"], "chat-1")
        self.assertEqual(run.call_args.kwargs["working_directory"], str(self.project.resolve()))
        self.assertTrue(run.call_args.kwargs["strict_resume"])

    def test_bridge_resolves_without_constructing_an_engine(self):
        from xuloom.desktop.bridge import DesktopBridge
        self.record(self.project)
        bridge = DesktopBridge(lambda _: None, self.project)
        with patch.object(bridge, "ensure_engine", side_effect=AssertionError("Must stay read-only")):
            result = asyncio.run(bridge.dispatch("chat.resolve", {"url": chat_link(self.project, "chat-1")}))
        self.assertEqual(result["session_id"], "chat-1"); self.assertIsNone(bridge.engine)


class RemoteChatLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"; self.settings = self.root / "data/workbench.json"
        self.profile = validate_profile({"id": "lab", "name": "名称", "host": "user@alias", "cwd": "/home/科学", "python": "python3"})
        for context in (patch.dict(os.environ, {"XULOOM_DATA_DIR": str(self.root/"data")}), patch("xuloom.core.config.CONFIG_FILE", self.config)):
            context.start(); self.addCleanup(context.stop)
        self.save_profile(self.profile)
        self.url = remote_chat_link(self.profile, "remote-chat")
        self.identity = "ssh:" + profile_key(self.profile) + ":remote-chat"

    def save_profile(self, profile): atomic_json(self.config, {"ssh_connections": [profile] if profile else [], "do_not_serialize": "PRIVATE_KEY"})

    def record(self):
        row = {"id": self.identity, "sessionId": "remote-chat", "binding": self.profile,
               "machine": {"hostname": "known-machine", "platform": "linux", "cwd": "/home/科学"}, "draft": "PRIVATE_DRAFT"}
        atomic_json(self.settings, {"remoteChats": {self.identity: row}})
        return row

    def test_canonical_identity_matches_desktop_and_resolution_never_connects_or_copies_drafts(self):
        self.assertEqual(profile_key(self.profile), "6d61e4e926e6a8a6aa62e24f3c97cef7190b6d286fc34ca3fb07ca05c5932231")
        self.assertEqual(profile_key({**self.profile, "name": "renamed"}), profile_key(self.profile))
        self.record(); original = self.settings.read_bytes()
        with patch("xuloom.remote.ssh.SSHClient.start", side_effect=AssertionError("resolution must not connect")):
            result = resolve_link(self.url, require_openable=True)
        self.assertEqual(result["transport"], "ssh"); self.assertFalse(result["remote_verified"])
        self.assertEqual(result["machine"]["hostname"], "known-machine")
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("user@alias", self.url)
        self.assertEqual(resolve_link(self.url)["binding"], self.profile)
        for suffix in ("\n", "?host=attacker", "#run", "/", "%20"):
            with self.assertRaises(ChatLinkError): resolve_link(self.url + suffix)

    def test_unknown_target_cannot_import_a_profile_and_corrupt_references_fail_closed(self):
        self.assertIsNone(resolve_link(self.url)["machine"])
        self.assertFalse(self.settings.exists())
        self.save_profile(None)
        with self.assertRaises(ChatLinkError) as missing: resolve_link(self.url)
        self.assertEqual(missing.exception.code, "not_found")
        self.save_profile(self.profile); row = self.record(); row["sessionId"] = "wrong"
        atomic_json(self.settings, {"remoteChats": {self.identity: row}})
        with self.assertRaises(ChatLinkError) as invalid: resolve_link(self.url)
        self.assertEqual(invalid.exception.code, "history_unreadable")
        atomic_json(self.settings, [])
        with self.assertRaises(ChatLinkError): resolve_link(self.url)

    def test_archive_deleted_connection_and_changed_target_do_not_retarget_links(self):
        row = self.record(); row["archived"] = True
        atomic_json(self.settings, {"remoteChats": {self.identity: row}})
        self.assertTrue(resolve_link(self.url)["archived"])
        with self.assertRaises(ChatLinkError) as failure: resolve_link(self.url, require_openable=True)
        self.assertEqual(failure.exception.code, "archived")
        self.save_profile({**self.profile, "cwd": "/other"})
        with self.assertRaises(ChatLinkError) as changed: resolve_link(self.url)
        self.assertEqual(changed.exception.code, "remote_target_changed")
        self.save_profile(None)
        with self.assertRaises(ChatLinkError) as missing: resolve_link(self.url)
        self.assertEqual(missing.exception.code, "connection_missing")

    def test_cli_chat_uses_ssh_adapter_never_a_local_working_directory(self):
        from xuloom.main import main
        self.record()
        with patch.object(sys, "argv", ["xuloom", "--chat", self.url]), patch("xuloom.remote.terminal.interactive_chat", new_callable=AsyncMock, return_value={"ok": True}) as remote, patch("xuloom.main.interactive_loop", side_effect=AssertionError("must not open local engine")):
            with self.assertRaises(SystemExit) as status: main()
        self.assertEqual(status.exception.code, 0)
        self.assertEqual(remote.call_args.args, ("lab",))
        self.assertEqual(remote.call_args.kwargs["session_id"], "remote-chat")
        self.assertEqual(remote.call_args.kwargs["machine"]["hostname"], "known-machine")
