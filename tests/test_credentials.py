"""Real current-user protection and failed-save recovery; owned fixtures only."""
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from xuloom.core import config
from xuloom.core.model_catalog import public_config
from xuloom.security.credentials import CredentialUnavailable, MARKER, is_protected, transform


@unittest.skipUnless(os.name == "nt", "Windows current-user DPAPI")
class CredentialTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="xuloom-credentials-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "config.json"
        environment = patch.dict(os.environ, {"HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
            "XULOOM_CONFIG_DIR": str(self.root), "XULOOM_DATA_DIR": str(self.root / "data"), "XULOOM_SKILLS_PATHS": "", "XULOOM_CREDENTIAL_STORAGE":"auto"})
        environment.start(); self.addCleanup(environment.stop)
        fixture = patch.object(config, "CONFIG_FILE", self.path)
        fixture.start(); self.addCleanup(fixture.stop)
        self.key = "owned-private-key-credentials-fixture"
        self.settings = {"model_provider": "openai", "openai_model": "fixture", "openai_base_url": "http://127.0.0.1:1/v1",
                         "openai_api_key": self.key, "model_connections": [{"id": "owned", "provider": "openai",
                         "api_key": self.key, "base_url": "http://127.0.0.1:1/v1", "models": ["fixture"], "model": "fixture"}],
                         "active_model_connection": "owned"}

    def raw(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_native_roundtrip_migrates_on_save_without_mutating_callers(self):
        self.path.write_text(json.dumps(self.settings), encoding="utf-8")
        before = self.path.read_bytes()
        self.assertEqual(config.load_config(), self.settings)
        self.assertEqual(self.path.read_bytes(), before, "Read-only discovery does not migrate")
        settings = copy.deepcopy(self.settings)
        config.update_config(lambda value: value)
        self.assertEqual(config.load_config(), settings)
        self.assertEqual(self.settings, settings)
        self.assertNotIn(self.key, self.path.read_text())
        self.assertTrue(is_protected(self.raw()["openai_api_key"]))
        self.assertTrue(public_config(config.load_config())["hasKey"])

    def test_long_unicode_fields_roundtrip_without_a_vault_blob_limit(self):
        settings = {"api_key": "汉字-key-" * 4096, "refresh_token": "token" * 2048, "ordinary": "leave"}
        config.save_config(settings)
        self.assertEqual(config.load_config(), settings)
        self.assertNotIn(settings["refresh_token"], self.path.read_text())

    def test_failed_native_protection_and_publish_preserve_the_original(self):
        config.save_config(self.settings)
        before = self.path.read_bytes()
        with patch("xuloom.security.credentials.native", side_effect=CredentialUnavailable()):
            with self.assertRaises(CredentialUnavailable): config.save_config({**self.settings, "model_provider": "changed"})
        self.assertEqual(self.path.read_bytes(), before)
        with patch("xuloom.core.storage.os.replace", side_effect=OSError("owned failed publish")):
            with self.assertRaises(OSError): config.save_config({**self.settings, "model_provider": "changed"})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".migration-*")), [])

    def test_unavailable_credentials_remain_editable_and_cannot_make_a_request(self):
        config.save_config(self.settings)
        raw = self.raw()
        raw["model_connections"][0]["api_key"]["data"] = "invalid!"
        raw["openai_api_key"]["data"] = "invalid!"
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        value = config.load_config()
        public = public_config(value)
        self.assertEqual(public["baseUrl"], self.settings["openai_base_url"])
        self.assertFalse(public["hasKey"])
        self.assertTrue(public["credentialUnavailable"])
        self.assertEqual(len(public["credentialStorage"]["unavailable"]), 2)
        self.assertNotIn("invalid!", json.dumps(public))
        config.update_config(lambda cfg: {**cfg, "unrelated": "keep"})
        self.assertEqual(self.raw()["openai_api_key"], raw["openai_api_key"])
        from xuloom.core.network import create_model_client
        with patch("openai.AsyncOpenAI") as sdk:
            with self.assertRaises(CredentialUnavailable):
                create_model_client(provider="openai", model="fixture", api_base_url=self.settings["openai_base_url"],
                                    api_key=value["openai_api_key"], network_mode="direct")
            sdk.assert_not_called()
        from xuloom.agents.connections import resolve_connection
        with self.assertRaises(CredentialUnavailable): resolve_connection("owned")

    def test_protection_is_bound_to_the_file_and_field(self):
        config.save_config(self.settings)
        raw = self.raw()
        raw["model_connections"][0]["api_key"] = raw["openai_api_key"]
        self.assertTrue(is_protected(transform(raw, self.path)["model_connections"][0]["api_key"]))
        self.assertTrue(is_protected(transform(self.raw(), self.root / "another.json")["openai_api_key"]))

    def test_unused_unavailable_provider_does_not_block_the_selected_connection(self):
        settings = {**self.settings, "anthropic_api_key": {MARKER:"future-backend","data":"unavailable"}}
        config.save_config(settings)
        with patch.dict(os.environ,{"OPENAI_API_KEY":"","ANTHROPIC_API_KEY":""}):
            config.load_config_into_env()
            self.assertEqual(os.environ["OPENAI_API_KEY"],self.key)
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"],"")

    def test_mcp_env_headers_and_url_query_are_protected_and_repairable(self):
        from xuloom.protocols.mcp_config import MCPConfigStore
        store = MCPConfigStore(self.root / ".xuloom/mcp_servers.json")
        store.save("web", {"url": "https://example.invalid/mcp?token=owned-query-secret", "headers": {"Authorization": self.key}})
        store.save("local", {"command": sys.executable, "env": {"OWNED_TOKEN": self.key, "MODE": "normal"}})
        self.assertNotIn(self.key, store.path.read_text())
        self.assertNotIn("owned-query-secret", store.path.read_text())
        self.assertEqual(store.read()["mcpServers"]["web"]["headers"]["Authorization"], self.key)
        snapshot = store.snapshot()[0]
        self.assertNotIn(self.key, json.dumps(snapshot))
        store.save("web", snapshot, existing=True)
        self.assertIn("owned-query-secret", store.read()["mcpServers"]["web"]["url"])
        data = json.loads(store.path.read_text())
        data["mcpServers"]["web"]["headers"]["Authorization"]["data"] = "broken!"
        store.path.write_text(json.dumps(data))
        snapshot = store.snapshot()[0]
        self.assertIn("凭据不可用", snapshot["error"])
        self.assertNotIn("broken!", json.dumps(snapshot))
        snapshot["headers"]["Authorization"] = "repaired-owned-key"
        store.save("web", snapshot, existing=True)
        self.assertEqual(store.read()["mcpServers"]["web"]["headers"]["Authorization"], "repaired-owned-key")

    def test_media_metadata_survives_but_generation_rejects_an_unavailable_key(self):
        from xuloom.media import config as media
        settings = {"media_connections": [{"id": "owned-media", "protocol": "auto", "base_url": "http://127.0.0.1:1/v1",
                    "api_key": self.key, "models": [{"id": "image", "kind": "image", "model": "fixture"}]}]}
        config.save_config(settings)
        raw = self.raw(); raw["media_connections"][0]["api_key"]["data"] = "broken!"
        self.path.write_text(json.dumps(raw))
        row = media.public_models()[0]
        self.assertTrue(row["credentialUnavailable"])
        self.assertFalse(row["hasKey"])
        with self.assertRaises(CredentialUnavailable): media.resolve("owned-media", "image")

    def test_actual_cli_repairs_from_an_environment_variable_without_echoing_it(self):
        self.path.write_text(json.dumps(self.settings))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "ANTHROPIC_", "XULOOM_TEST_"))}
        env.update(XULOOM_CONFIG_DIR=str(self.root), XULOOM_DATA_DIR=str(self.root / "data"),
                   HOME=str(self.root / "home"), USERPROFILE=str(self.root / "home"), OWNED_REPAIR_KEY="owned-new-key-no-echo")
        def run(*args):
            result = subprocess.run([sys.executable, "-m", "xuloom", "credentials", *args, "--json"],
                env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(self.key, result.stdout + result.stderr)
            self.assertNotIn(env["OWNED_REPAIR_KEY"], result.stdout + result.stderr)
            return json.loads(result.stdout)["result"]
        self.assertEqual(run("status")["credentials"]["mode"], "windows-dpapi")
        run("migrate")
        self.assertNotIn(self.key, self.path.read_text())
        raw = self.raw(); raw["model_connections"][0]["api_key"]["data"] = "broken!"
        self.path.write_text(json.dumps(raw))
        run("set-model-key", "--connection", "owned", "--key-env", "OWNED_REPAIR_KEY")
        self.assertEqual(config.load_config()["openai_api_key"], env["OWNED_REPAIR_KEY"])
        self.assertNotIn(env["OWNED_REPAIR_KEY"], self.path.read_text())


class CredentialContractTests(unittest.TestCase):
    def test_bad_header_credentials_are_rejected_before_sdk_construction_without_echo(self):
        from xuloom.core.network import create_model_client
        secret="owned-key\r\nX-Header: secret"
        with patch("openai.AsyncOpenAI") as sdk:
            with self.assertRaises(ValueError) as error:
                create_model_client(provider="openai",model="fixture",api_base_url="http://127.0.0.1:1/v1",api_key=secret,network_mode="direct")
            self.assertNotIn(secret,str(error.exception))
            sdk.assert_not_called()

    def test_extension_owned_documents_are_not_reinterpreted_as_credentials(self):
        value = {"extension": {"api_key": {"type": "string"}, "password": {"description": "schema"}}}
        self.assertEqual(transform(value,"unused.json",protect=True),value)

    def test_unknown_protection_is_preserved_and_never_treated_as_a_key(self):
        value = {MARKER: "future-backend", "data": "owned"}
        self.assertEqual(transform({"api_key": value}, "unused.json"), {"api_key": value})
        from xuloom.security.credentials import require_secret
        with self.assertRaises(CredentialUnavailable): require_secret(value)

    def test_concurrent_mcp_edits_merge_and_store_only_protected_fields(self):
        with tempfile.TemporaryDirectory(prefix="xuloom-mcp-edits-") as folder:
            root = Path(folder)
            target = root / ".xuloom/mcp_servers.json"
            code = ("import sys,time;from xuloom.protocols.mcp_config import MCPConfigStore;"
                    "MCPConfigStore(sys.argv[1]).save(sys.argv[2],{'command':sys.executable,'env':{'TOKEN':'owned-mcp-key'}})")
            env = {**os.environ, "HOME": str(root / "home"), "USERPROFILE": str(root / "home"),
                   "XULOOM_CONFIG_DIR": str(root / "config"), "XULOOM_DATA_DIR": str(root / "data"), "XULOOM_SKILLS_PATHS": ""}
            processes = [subprocess.Popen([sys.executable, "-c", code, str(target), "worker" + str(i)],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for i in range(4)]
            try:
                for process in processes:
                    _, error = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, 0, error.decode(errors="replace"))
            finally:
                for process in processes:
                    if process.poll() is None: process.kill(); process.wait()
            from xuloom.protocols.mcp_config import MCPConfigStore
            self.assertEqual(len(MCPConfigStore(target).read()["mcpServers"]), 4)
            if os.name == "nt": self.assertNotIn("owned-mcp-key", target.read_text())

    def test_formal_cleanup_entry_works_without_model_configuration(self):
        with tempfile.TemporaryDirectory(prefix="xuloom-cleanup-entry-") as folder:
            env = {k:v for k,v in os.environ.items() if not k.startswith(("OPENAI_","ANTHROPIC_"))}
            env.update(HOME=folder, USERPROFILE=folder, XULOOM_CONFIG_DIR=folder, XULOOM_DATA_DIR=folder, XULOOM_SKILLS_PATHS="")
            result = subprocess.run([sys.executable,"-m","xuloom","sandbox-cleanup","--cwd",folder,"--json"],
                env=env,capture_output=True,text=True,encoding="utf-8",timeout=30,
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)["result"]["cleanup_issues"],[])

    def test_unavailable_mcp_auth_does_not_open_any_transport(self):
        from xuloom.protocols.mcp_bridge import MCP_AVAILABLE, MCPBridge
        if not MCP_AVAILABLE: self.skipTest("Optional MCP SDK")
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"mcp_servers.json"
            path.write_text(json.dumps({"mcpServers":{"owned":{"url":"https://example.invalid/mcp",
                "headers":{"Authorization":{MARKER:"future-backend","data":"not-a-key"}}}}}))
            async def run():
                bridge=MCPBridge(str(path))
                try:
                    with patch("xuloom.protocols.mcp_bridge.httpx.AsyncClient") as transport:
                        await bridge.load_and_connect()
                        transport.assert_not_called()
                    self.assertEqual(bridge.statuses["owned"]["status"],"error")
                finally: await bridge.cleanup()
            asyncio.run(run())
