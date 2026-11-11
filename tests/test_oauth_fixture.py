"""The desktop OAuth fixture owns its callback port until the client is ready."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest


class OAuthFixtureTests(unittest.TestCase):
    def test_fixed_callback_port_has_an_explicit_handoff(self):
        fixture = Path(__file__).resolve().parent / "fixtures/oauth-server.py"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "ANTHROPIC_"))}
        with ThreadPoolExecutor(max_workers=1) as reader, subprocess.Popen(
                [sys.executable, "-u", str(fixture)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
            try:
                line = reader.submit(process.stdout.readline).result(timeout=20)
                address = json.loads(line)
                from urllib.parse import urlsplit
                port = urlsplit(address["fixed"]["redirect_uri"]).port
                with socket.socket() as probe:
                    with self.assertRaises(OSError, msg="Callback port was released before the client was ready"):
                        probe.bind(("127.0.0.1", port))
                process.stdin.write("release-fixed\n"); process.stdin.flush()
                ack = reader.submit(process.stdout.readline).result(timeout=10)
                self.assertEqual(json.loads(ack), {"fixed_callback_released": True})
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", port))
            finally:
                try:
                    process.communicate(input="stop\n", timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.communicate()


if __name__ == "__main__":
    unittest.main()
