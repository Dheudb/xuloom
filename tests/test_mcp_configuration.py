"""Malformed extension settings must fail before opening an execution transport."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xuloom.protocols.mcp_config import MCPConfigStore, validate_server
from xuloom.protocols.mcp_bridge import MCPBridge, MCP_AVAILABLE
from xuloom.desktop.bridge import DesktopBridge


class MCPConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix="xuloom-mcp-config-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.store = MCPConfigStore(self.root / ".xuloom/mcp_servers.json")
        self.store.path.parent.mkdir(parents=True)

    def write_raw(self, servers):
        self.store.path.write_text(json.dumps({"mcpServers": servers, "unrelated": [1, 2]}), encoding="utf-8")
        return self.store.path.read_bytes()

    def test_enabled_requires_boolean_and_omission_remains_enabled(self):
        for value in ("false", "true", 0, 1, None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_server({"command": "owned-not-launched", "enabled": value})
        self.assertTrue(validate_server({"command": "node"})["enabled"])
        self.assertFalse(validate_server({"command": "node", "enabled": False})["enabled"])

    def test_explicit_transport_never_falls_back_to_executable(self):
        for value in (False, 0, None, "", [], {}, "sse"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_server({"type": value, "command": "owned-not-launched"})
        for value in (False, None, "", [], {}):
            with self.subTest(url=value), self.assertRaises(ValueError):
                validate_server({"url": value, "command": "owned-not-launched"})
        self.assertEqual(validate_server({"type": "streamable-http", "url": "https://example.invalid/mcp"})["type"], "http")

    def test_invalid_urls_fail_with_safe_errors(self):
        for value in ([], 9, {}, {"$xuloom_secret": "bad", "preview": []}, "https://example.invalid:bad/mcp", "https://example.invalid:65536", "https://:@example.invalid", "https://example.invalid/#", " https://example.invalid", "https://exam\nple.invalid", "https://example.invalid/a b"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    validate_server({"type": "http", "url": value})
                self.assertNotIn(str(value), str(raised.exception))

    def test_snapshot_is_read_only_and_invalid_row_cannot_echo_contents(self):
        before = self.write_raw({"bad": {"type": {"secret": "owned-secret-do-not-display"}, "enabled": "false"}, "good": {"command": "node"}})
        snapshot = self.store.snapshot()
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertNotIn("owned-secret", json.dumps(snapshot))
        bad, good = snapshot
        self.assertTrue(bad["error"])
        self.assertFalse(bad["enabled"])
        self.assertEqual(good["command"], "node")

    def test_edit_invalid_container_is_rejected_without_writing(self):
        before = self.write_raw({"owned": {"command": "node"}})
        for config in ([], None, {"command": "node", "env": []}, {"command": "node", "headers": None}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.store.save("owned", config, existing=True)
            self.assertEqual(self.store.path.read_bytes(), before)
        for existing in ("false", 1, None):
            with self.subTest(existing=existing), self.assertRaises(ValueError):
                self.store.save("owned", {"command": "node"}, existing=existing)
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_toggle_strict_and_repair_invalid_saved_container(self):
        before = self.write_raw({"owned": {"command": "node", "env": None}})
        for value in ("false", 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.toggle("owned", value)
            self.assertEqual(self.store.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.store.toggle("owned", True)
        self.store.toggle("owned", False)
        self.assertFalse(self.store.read()["mcpServers"]["owned"]["enabled"])
        self.store.save("owned", {"command": "node", "env": {"KEY": ""}}, existing=True)
        self.assertEqual(self.store.read()["mcpServers"]["owned"]["env"], {"KEY": ""})

    def test_malformed_skill_manifest_does_not_hide_healthy_services(self):
        broken = self.root / "broken"
        mixed = self.root / "mixed"
        for path in (broken, mixed):
            path.mkdir()
        (broken / "mcp_servers.json").write_text("{not-json", encoding="utf-8")
        (mixed / "mcp_servers.json").write_text(json.dumps({"mcpServers": {"invalid": {"command": "node", "args": [False]}, "healthy": {"command": "${python}", "args": ["${skill}/owned.py"]}}}), encoding="utf-8")
        self.write_raw({"project": {"command": "node"}})
        issues = []
        with patch("xuloom.skills.registry.SkillRegistry.active", return_value=[{"name": "broken", "root": str(broken)}, {"name": "mixed", "root": str(mixed)}]):
            rows = self.store.read(include_skills=True, issues=issues)["mcpServers"]
        self.assertEqual(set(rows), {"project", "healthy"})
        self.assertEqual(rows["healthy"]["command"], sys.executable)
        self.assertEqual(rows["healthy"]["args"], [str(mixed) + "/owned.py"])
        self.assertEqual(len(issues), 2)
        self.assertNotIn("{not-json", json.dumps(issues))

    def test_skill_manifest_shape_errors_are_local_and_repair_changes_stamp(self):
        mounted = self.root / "owned"
        mounted.mkdir()
        manifest = mounted / "mcp_servers.json"
        desktop = DesktopBridge(lambda event: None, self.root)
        active = [{"name": "owned", "root": str(mounted)}]
        with patch("xuloom.skills.registry.SkillRegistry.active", return_value=active):
            initial = desktop.config_stamp()
            for value in ([], {}, {"mcpServers": []}, {"mcpServers": {"owned": None}}, {"mcpServers": {"owned": {"type": []}}}):
                manifest.write_text(json.dumps(value), encoding="utf-8")
                snapshot = desktop.mcp_snapshot()
                self.assertEqual(snapshot["servers"], [])
                self.assertEqual(len(snapshot["configurationIssues"]), 1)
                self.assertNotEqual(desktop.config_stamp(), initial)
            broken = desktop.config_stamp()
            manifest.write_text(json.dumps({"mcpServers": {"owned": {"command": "node", "enabled": False}}}), encoding="utf-8")
            self.assertNotEqual(desktop.config_stamp(), broken)
            self.assertEqual(desktop.mcp_snapshot()["configurationIssues"], [])
            manifest.unlink()
            self.assertEqual(desktop.config_stamp(), initial)

    def test_formal_cli_lists_diagnostics_without_launching_bad_extension(self):
        mounted = self.store.path.parent / "skills" / "owned-config-check"
        mounted.mkdir(parents=True)
        (mounted / "SKILL.md").write_text("---\nname: owned-config-check\ndescription: Owned configuration fixture\n---\nLocal test.\n", encoding="utf-8")
        (mounted / "mcp_servers.json").write_text(json.dumps({"mcpServers": {"bad": {"command": "owned-never-started", "enabled": "false", "env": {"TOKEN": "owned-do-not-echo"}}}}), encoding="utf-8")
        before = self.write_raw({"working": {"command": "owned-not-launched", "enabled": False}})
        result = subprocess.run([sys.executable, "-m", "xuloom", "mcp", "list", "--cwd", str(self.root), "--json"], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("owned-do-not-echo", result.stdout + result.stderr)
        payloads = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        receipt = payloads[-1]["result"]
        self.assertEqual(len(receipt["configurationIssues"]), 1)
        self.assertEqual([row["name"] for row in receipt["servers"]], ["working"])
        self.assertEqual(self.store.path.read_bytes(), before)


class MCPBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture = tempfile.TemporaryDirectory(prefix="xuloom-mcp-boundary-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.store = MCPConfigStore(self.root / ".xuloom/mcp_servers.json")
        self.store.path.parent.mkdir(parents=True)

    async def test_desktop_rpc_rejects_instead_of_coercing_boolean(self):
        self.store.write({"mcpServers": {"owned": {"command": "node"}}})
        before = self.store.path.read_bytes()
        desktop = DesktopBridge(lambda event: None, self.root)
        for method, params in (("mcp.toggle", {"name": "owned", "enabled": "false"}), ("mcp.toggle", {"name": "owned"}), ("mcp.save", {"name": "owned", "existing": "true", "config": {"command": "other"}})):
            with self.subTest(method=method, params=params), self.assertRaises(ValueError):
                await desktop.dispatch(method, params)
            self.assertEqual(self.store.path.read_bytes(), before)

    async def test_include_disabled_flag_never_accepts_a_truthy_string(self):
        bridge = MCPBridge(str(self.store.path))
        with self.assertRaises(ValueError):
            await bridge.load_and_connect(include_disabled="false")

    @unittest.skipUnless(MCP_AVAILABLE and shutil.which("node"), "Requires MCP SDK and Node")
    async def test_invalid_rows_never_open_transports_and_valid_sibling_runs(self):
        fixture = str(Path(__file__).parent / "fixtures/mcp-server.cjs")
        invalid = {"badflag": {"command": "owned-not-launched", "enabled": "false"}, "badtype": {"type": [], "command": "owned-not-launched"}, "badurl": {"type": "http", "url": "https://example.invalid:bad/mcp"}, "unavailable": {"type": "http", "url": {"$xuloom_secret": "bad"}}}
        invalid["../invalid-name"] = {"command": "owned-not-launched"}
        self.store.write({"mcpServers": invalid})
        bridge = MCPBridge(str(self.store.path))
        with patch("xuloom.protocols.mcp_bridge.stdio_client") as stdio, patch("xuloom.protocols.mcp_bridge.httpx.AsyncClient") as http:
            try:
                self.assertEqual(await bridge.load_and_connect(), {})
                stdio.assert_not_called()
                http.assert_not_called()
                self.assertTrue(all(row["status"] == "error" for row in bridge.statuses.values()))
            finally:
                await bridge.cleanup()
        self.store.write({"mcpServers": {**invalid, "healthy": {"command": shutil.which("node"), "args": [fixture]}}})
        bridge = MCPBridge(str(self.store.path))
        try:
            tools = await bridge.load_and_connect()
            self.assertIn("healthy_search", tools)
            self.assertTrue((await tools["healthy_search"](query="owned-proof"))["ok"])
        finally:
            await bridge.cleanup()
