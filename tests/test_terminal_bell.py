import os
import unittest
from unittest.mock import patch

from xuloom.main import BELL_AFTER_SECONDS, should_ring_bell


class TerminalBellTests(unittest.TestCase):
    def test_rings_only_after_long_turns_in_a_terminal(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XULOOM_TERMINAL_BELL", None)
            self.assertTrue(should_ring_bell(BELL_AFTER_SECONDS, True))
            self.assertFalse(should_ring_bell(BELL_AFTER_SECONDS - 0.1, True))
            self.assertFalse(should_ring_bell(BELL_AFTER_SECONDS * 3, False))

    def test_environment_can_disable_the_bell(self):
        with patch.dict(os.environ, {"XULOOM_TERMINAL_BELL": "0"}):
            self.assertFalse(should_ring_bell(BELL_AFTER_SECONDS * 3, True))


if __name__ == "__main__":
    unittest.main()
