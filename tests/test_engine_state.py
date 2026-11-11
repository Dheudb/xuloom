import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class EngineStateTests(unittest.TestCase):
    """xuloom/core/engine_state.py types the engine mixins; it must stay generated and inert."""

    def test_generated_state_matches_the_engine(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/gen_engine_state.py"), "--check"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_state_is_empty_at_runtime_and_last_before_object(self):
        from xuloom.core.engine import QueryEngine
        from xuloom.core.engine_state import EngineState
        self.assertEqual([name for name in vars(EngineState) if not name.startswith("__")], [])
        self.assertEqual(QueryEngine.__mro__[-2:], (EngineState, object))


if __name__ == "__main__":
    unittest.main()
