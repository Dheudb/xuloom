"""Actual legacy background processes obey native worker ownership and stop."""
import asyncio
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from xuloom.core.engine import QueryEngine
from xuloom.memory.memory import MemoryCompactor
from xuloom.utils.shell_tasks import ShellTaskManager


class ShellOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_cancel_stops_only_its_legacy_background_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"HOME": str(root / "home"), "USERPROFILE": str(root / "home"),
                    "XULOOM_SKILLS_PATHS": "", "XULOOM_DATA_DIR": str(root / "data")}), \
                    patch("xuloom.core.config.CONFIG_FILE", root / "config.json"), \
                    patch.object(MemoryCompactor, "_resolve_token_encoder", return_value=None):
                engine = QueryEngine(cwd=directory, model="fixture", model_provider="openai", api_key="fixture", api_base_url="http://127.0.0.1:1/v1")
                manager = engine.shell_task_manager
                argv = [sys.executable, "-c", "import time; time.sleep(60)"]
                command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
                try:
                    first = await manager.start_task(command, cwd=directory, owner="worker-first")
                    second = await manager.start_task(command, cwd=directory, owner="worker-second")
                    from xuloom.tools.task_tools import TaskKillTool, TaskListTool
                    kill, listing = TaskKillTool(), TaskListTool()
                    kill.context = listing.context = {"shell_task_manager": manager, "agent_depth": 1, "agent_id": "worker-first"}
                    await kill.execute(task_id=second["task_id"])
                    self.assertIsNone(manager.tasks[second["task_id"]]["process"].returncode)
                    visible = await listing.execute()
                    self.assertNotIn(second["task_id"], str(visible))
                    engine.register_subagent_record({"agent_id": "worker-first", "status": "completed", "executor": "local"})
                    await engine.available_tools["agent_tool"].execute(action="cancel", agent_id="worker-first")
                    self.assertIsNotNone(manager.tasks[first["task_id"]]["process"].returncode)
                    self.assertIsNone(manager.tasks[second["task_id"]]["process"].returncode)
                    restored = ShellTaskManager(str(root / "restored")); restored.load_state(manager.export_state())
                    await restored.close(owner="worker-second")
                    self.assertIsNone(manager.tasks[second["task_id"]]["process"].returncode, "A saved PID is not authority to kill a live process")
                    await engine.request_abort()
                    self.assertIsNotNone(manager.tasks[second["task_id"]]["process"].returncode)
                finally:
                    await engine.aclose()
