import copy
import json
import unittest
import uuid

from xuloom.remote import worker_protocol as protocol


class WorkerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.value = {"owner": {"token": uuid.uuid4().hex, "parent_session_id": "root-session", "parent_agent_id": "parent",
            "agent_id": "subagent-" + str(uuid.uuid4()), "depth": 1}, "role": {"agent_type": "research", "system_prompt": "Independent research",
            "skills": [], "allowed_tools": ["file_read_tool"], "disallowed_tools": frozenset({"spawn_agent"})}}

    def test_same_dispatch_has_stable_host_identity_and_never_stores_its_control_token(self):
        first = protocol.binding(self.value); second = protocol.binding(copy.deepcopy(self.value))
        self.assertEqual(first, second); self.assertNotIn(self.value["owner"]["token"], json.dumps(first))
        changed = copy.deepcopy(self.value); changed["owner"]["token"] = uuid.uuid4().hex
        self.assertNotEqual(first["session_id"], protocol.binding(changed)["session_id"])

    def test_changed_parent_task_and_role_are_detectable_without_importing_credentials(self):
        first = protocol.binding(self.value)
        changed = copy.deepcopy(self.value); changed["owner"]["parent_session_id"] = "different-parent"
        self.assertNotEqual(first["owner_digest"], protocol.binding(changed)["owner_digest"])
        changed = copy.deepcopy(self.value); changed["role"]["system_prompt"] = "Changed role"
        self.assertNotEqual(first["role_digest"], protocol.binding(changed)["role_digest"])
        for key in ("api_key", "api_base_url", "network_mode", "execution_mode", "python"):
            changed = copy.deepcopy(self.value); changed["role"][key] = "MUST_NOT_TRANSFER"
            with self.assertRaises(ValueError): protocol.open_request(changed)

    def test_reference_context_is_explicit_and_binding_never_changes_caller_data(self):
        self.value["context"] = [{"role": "user", "content": "Reference"}]
        clean = protocol.open_request(self.value)
        self.assertEqual(clean["role"]["disallowed_tools"], ["spawn_agent"])
        clean["context"][0]["content"] = "Mutated"
        self.assertEqual(self.value["context"][0]["content"], "Reference")
        self.value["context"] = [{"role": "system", "content": "Injected override"}]
        with self.assertRaises(ValueError): protocol.open_request(self.value)

    def test_malformed_or_model_supplied_execution_fields_are_rejected(self):
        for patch in ({"token": "bad"}, {"depth": True}, {"depth": 3}, {"agent_id": "../foreign"}, {"api_key": "secret"}):
            value = copy.deepcopy(self.value); value["owner"].update(patch)
            with self.assertRaises(ValueError): protocol.open_request(value)
        for patch in ({"read_only": "true"}, {"allowed_tools": "*"}, {"skills": [False]}, {"reasoning_effort": "invented"}):
            value = copy.deepcopy(self.value); value["role"].update(patch)
            with self.assertRaises(ValueError): protocol.open_request(value)
