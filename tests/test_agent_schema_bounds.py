import unittest

from xuloom.agents.agent_tool import AgentToolInput, SpawnAgentInput


class AgentSchemaBoundsTests(unittest.TestCase):
    """The desktop subagent E2E no longer sees agent schemas up front (they are deferred), so pin the bounds here."""

    def test_parallel_count_is_bounded_to_ten_for_both_launch_surfaces(self):
        for model in (AgentToolInput, SpawnAgentInput):
            count = model.model_json_schema()["properties"]["count"]
            self.assertEqual((count["minimum"], count["maximum"], count["default"]), (1, 10, 1), model.__name__)


if __name__ == "__main__":
    unittest.main()
