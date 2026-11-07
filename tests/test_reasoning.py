"""Model controls exercised at catalog, native request, switching and child boundaries."""
import copy
import json
import unittest
from unittest.mock import patch

import test_tool_recovery as recovery
from xuloom.core.engine import QueryEngine
from xuloom.core.model_catalog import save_connection, select_model, select_reasoning, public_config
from xuloom.core.reasoning import normalize_profile, configured_profile, request_options


def profile(adapter="deepseek", **values):
    provider = "anthropic" if adapter.startswith("anthropic") else "openai"
    return normalize_profile({"adapter": adapter, **values}, provider)


class ReasoningTests(unittest.TestCase):
    def test_adapters_emit_only_native_parameters_and_legacy_switch_defaults_mean_enabled(self):
        for adapter, expected in [
            ("openai", {"reasoning_effort": "high"}),
            ("deepseek", {"reasoning_effort": "high", "extra_body": {"thinking": {"type": "enabled"}}}),
            ("qwen", {"reasoning_effort": "high", "extra_body": {"enable_thinking": True}}),
            ("qwen-local", {"reasoning_effort": "high", "extra_body": {"chat_template_kwargs": {"enable_thinking": True}}}),
            ("anthropic", {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}),
        ]:
            provider = "anthropic" if adapter == "anthropic" else "openai"
            p = profile(adapter, efforts=["low", "high"], effort="high", thinking="default" if adapter == "openai" else "enabled")
            self.assertEqual(request_options(p, provider), expected)
            enabled_options = {key: value for key, value in expected.items() if key not in {"reasoning_effort", "output_config"}}
            self.assertEqual(request_options(profile(adapter), provider), enabled_options)
            self.assertEqual(profile(adapter)["thinking"], "default" if adapter == "openai" else "enabled")
        self.assertEqual(request_options(profile("anthropic-budget", thinking="enabled"), "anthropic", max_tokens=4096),
            {"thinking": {"type": "enabled", "budget_tokens": 2048}})

    def test_disabled_thinking_suppresses_conflicting_effort_without_losing_preference(self):
        for adapter in ("deepseek", "qwen", "qwen-local", "anthropic"):
            p = profile(adapter, efforts=["high"], effort="high", thinking="disabled", canDisable=True)
            options = request_options(p, "anthropic" if adapter == "anthropic" else "openai")
            self.assertNotIn("reasoning_effort", options)
            self.assertNotIn("output_config", options)
            self.assertEqual(p["effort"], "high")
        with self.assertRaises(ValueError): profile("anthropic", canDisable=False, thinking="disabled")
        with self.assertRaises(ValueError): request_options(profile("anthropic-budget", thinking="enabled"), "anthropic", max_tokens=1024)
        with self.assertRaises(ValueError): request_options(profile(efforts=["high"]), "openai", effort_override="xhigh")
        with self.assertRaises(ValueError): normalize_profile({"adapter": "deepseek"}, "anthropic")
        self.assertEqual(request_options(None, "openai"), {})

    def test_catalog_isolates_same_model_across_connections_and_validates_stale_selection(self):
        cfg = save_connection({}, {"provider": "openai", "connectionId": "one", "model": "shared", "apiKey": "fixture",
            "baseUrl": "https://fixture.invalid/v1", "modelReasoning": {"shared": profile(efforts=["low", "high"])}})
        cfg = save_connection(cfg, {"provider": "openai", "connectionId": "two", "model": "shared", "apiKey": "fixture2",
            "baseUrl": "https://fixture.invalid/v1", "modelReasoning": {"shared": profile("qwen")}})
        cfg = select_reasoning(cfg, {"connectionId": "two", "model": "shared", "thinking": "disabled"})
        with patch("xuloom.core.config.load_config", return_value=cfg):
            self.assertEqual(configured_profile("openai", "shared", "https://fixture.invalid/v1")["adapter"], "qwen")
        cfg = select_model(cfg, "one", "shared")
        self.assertEqual(public_config(cfg)["modelReasoning"]["shared"]["thinking"], "enabled")
        with self.assertRaises(ValueError): select_reasoning(cfg, {"connectionId": "two", "model": "shared", "thinking": "enabled"})
        self.assertNotIn("api_key", json.dumps(public_config(cfg)))


class ReasoningWireTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    asyncTearDown = recovery.RecoveryTests.asyncTearDown
    fixture = recovery.RecoveryTests.fixture

    async def test_selected_controls_reach_actual_request_and_do_not_change_collaboration(self):
        self.engine.set_collaboration_mode("ultra")
        self.engine.reasoning_profile = profile(efforts=["high"], effort="high", thinking="disabled")
        self.fixture(['ANSWER'])
        result = await self.engine._run_loop("A direct question.", sys_print_callback=lambda _: None)
        self.assertTrue(result.ok)
        self.assertEqual(self.requests[0]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn("reasoning_effort", self.requests[0])
        self.assertEqual(self.engine.collaboration_mode, "ultra")
        self.assertEqual(self.engine.subagent_registry, [])

    async def test_switching_and_subagent_model_override_do_not_leak_reasoning_settings(self):
        cfg = save_connection({}, {"provider": "openai", "connectionId": "fixture", "models": ["fixture", "other"],
            "model": "fixture", "apiKey": "fixture", "baseUrl": self.engine.api_base_url,
            "modelReasoning": {"fixture": profile("qwen", thinking="disabled")}})
        old_client, old_compactor = self.engine.client, self.engine.compactor
        with patch("xuloom.core.config.load_config", return_value=cfg):
            self.engine.switch_model_runtime(model="fixture", persist=False)
            await old_client.close()
            await old_compactor.client.close()
            self.assertEqual(self.engine.reasoning_profile["adapter"], "qwen")
            self.engine.reasoning_effort = "high"
            self.engine.tool_context["reasoning_effort"] = "high"
            seen = []
            async def child_run(child, user_input, sys_print_callback):
                from xuloom.core.run_result import RunResult
                seen.append((child.primary_model, child.reasoning_effort, copy.deepcopy(child.reasoning_profile)))
                return RunResult("CHILD", "completed", True)
            self.engine.permission_manager.approval_policy.set_mode("full")
            with patch.object(QueryEngine, "run", child_run):
                await self.engine.available_tools["agent_tool"].execute(prompt="Independent task", model="other")
            self.assertEqual(seen, [("other", None, None)])
            old_client, old_compactor = self.engine.client, self.engine.compactor
            self.engine.switch_model_runtime(model="other", persist=False)
            await old_client.close()
            await old_compactor.client.close()
            self.assertIsNone(self.engine.reasoning_profile)
            self.assertIsNone(self.engine.reasoning_effort)

    async def test_replay_identity_includes_reasoning_selection(self):
        manager = self.engine.vcr_manager
        common = dict(model="fixture", system="", messages=[], tools=[], max_tokens=4096)
        one = manager.build_request_payload(**common, reasoning={"profile": profile(thinking="enabled")})
        two = manager.build_request_payload(**common, reasoning={"profile": profile(thinking="disabled")})
        self.assertNotEqual(manager._hash_request(one), manager._hash_request(two))

    async def test_same_model_children_inherit_session_choices_and_resume_keeps_them(self):
        self.engine.reasoning_profile = profile(efforts=["high"], effort="high", thinking="enabled")
        self.engine.set_reasoning_options(thinking="disabled")
        self.engine.permission_manager.approval_policy.set_mode("full")
        seen = []
        async def child_run(child, user_input, sys_print_callback):
            from xuloom.core.run_result import RunResult
            seen.append(copy.deepcopy(child.reasoning_profile))
            return RunResult("CHILD", "completed", True)
        with patch.object(QueryEngine, "run", child_run):
            await self.engine.available_tools["spawn_agent"].execute(prompt="Independent task")
            await self.engine.available_tools["agent_tool"].execute(action="wait")
        self.assertEqual(seen[0]["thinking"], "disabled")
        self.assertTrue(self.engine.persist_session_state())
        self.engine.session_manager.release_writer()
        self.engine.reasoning_profile = profile(efforts=["high"])
        self.assertTrue(self.engine.load_session(self.engine.session_id))
        self.assertEqual(self.engine.reasoning_profile["thinking"], "disabled")

    async def test_incompatible_worker_effort_is_rejected_before_registration(self):
        self.engine.reasoning_profile = profile(efforts=["high"])
        self.engine.tool_context["reasoning_profile"] = copy.deepcopy(self.engine.reasoning_profile)
        with self.assertRaisesRegex(ValueError, "不支持"):
            await self.engine.available_tools["agent_tool"].execute(prompt="Independent task", reasoning_effort="max")
        self.assertEqual(self.engine.subagent_registry, [])
