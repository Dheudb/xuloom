"""Thinking/reasoning configuration per engine and per mode."""
from typing import Optional
import copy
from xuloom.core.reasoning import EFFORTS, normalize_profile
from xuloom.core.session_lock import SessionBusyError
from xuloom.core.engine_state import EngineState


class ReasoningMixin(EngineState):
    """Thinking/reasoning configuration per engine and per mode."""

    def _normalize_thinking_config(self, config):
        if config is None or config is False or config == "disabled":
            return {"type": "disabled"}
        if config == "adaptive":
            return {"type": "adaptive", "display": "summarized"}
        if config == "enabled":
            return {"type": "enabled", "budget_tokens": 2048, "display": "summarized"}
        if not isinstance(config, dict):
            return None

        thinking_type = str(config.get("type", "")).strip().lower()
        display = str(config.get("display", "summarized")).strip().lower()
        if display not in {"summarized", "omitted"}:
            display = "summarized"

        if thinking_type == "disabled":
            return {"type": "disabled"}
        if thinking_type == "adaptive":
            return {"type": "adaptive", "display": display}
        if thinking_type == "enabled":
            budget_tokens = config.get("budget_tokens", 2048)
            try:
                budget_tokens = int(budget_tokens)
            except (TypeError, ValueError):
                budget_tokens = 2048
            budget_tokens = max(1024, budget_tokens)
            return {
                "type": "enabled",
                "budget_tokens": budget_tokens,
                "display": display,
            }
        return None

    def set_reasoning_options(self, *, thinking=None, effort=None):
        if self.has_active_turn() or any(not task.done() for task in self.tool_context["subagent_tasks"].tasks.values()):
            raise SessionBusyError("请先停止或等待任务，再切换推理设置")
        if self.reasoning_profile is None:
            if thinking is not None:
                raise ValueError("请先在模型设置中声明 Thinking 参数格式，不能根据模型别名猜测")
            if effort not in (None, "default") and effort not in EFFORTS:
                raise ValueError("Unsupported reasoning effort")
            if effort not in (None, "default") and self.model_provider != "openai":
                raise ValueError("请先声明 Anthropic 模型的 effort 能力")
            self.reasoning_effort = None if effort in (None, "default") else effort
        else:
            profile = {**self.reasoning_profile, **({"thinking": thinking} if thinking is not None else {}),
                **({"effort": effort} if effort is not None else {})}
            profile = normalize_profile(profile, self.model_provider)
            if profile != self.reasoning_profile:
                self._strip_thinking_signatures_from_history()
            self.reasoning_profile, self.reasoning_effort = profile, None
        self._sync_model_runtime_context()
        return copy.deepcopy(self.reasoning_profile or {"effort": self.reasoning_effort or "default"})

    def _thinking_config_to_text(self, config) -> str:
        normalized = self._normalize_thinking_config(config)
        if not normalized:
            return "disabled"
        if normalized["type"] == "enabled":
            return (
                f"enabled (budget_tokens={normalized.get('budget_tokens')}, "
                f"display={normalized.get('display', 'summarized')})"
            )
        if normalized["type"] == "adaptive":
            return f"adaptive (display={normalized.get('display', 'summarized')})"
        return "disabled"

    def set_thinking_config(self, config) -> dict:
        normalized = self._normalize_thinking_config(config)
        if normalized is None:
            raise ValueError("Unsupported thinking config.")
        self.default_thinking_config = normalized
        self._thinking_config_explicit = True
        self.tool_context["thinking_config_explicit"] = True
        self.tool_context["thinking_config"] = self.default_thinking_config
        return copy.deepcopy(self.default_thinking_config)

    def set_mode_thinking_config(self, mode: str, config) -> dict:
        mode_name = str(mode or "").strip()
        if not mode_name:
            raise ValueError("mode is required.")
        normalized = self._normalize_thinking_config(config)
        if normalized is None:
            self.mode_thinking_overrides.pop(mode_name, None)
        else:
            self.mode_thinking_overrides[mode_name] = normalized
        self.tool_context["mode_thinking_overrides"] = self.mode_thinking_overrides
        self._thinking_config_explicit = True
        self.tool_context["thinking_config_explicit"] = True
        return copy.deepcopy(self.mode_thinking_overrides)

    def get_thinking_config(self, mode: Optional[str] = None) -> dict:
        if mode is None:
            return copy.deepcopy(self._resolve_thinking_config())
        return copy.deepcopy(
            self.mode_thinking_overrides.get(str(mode), self.default_thinking_config)
        )

    def _resolve_thinking_config(self) -> dict:
        mode_name = self.get_mode()
        effective = self.mode_thinking_overrides.get(mode_name, self.default_thinking_config)
        normalized = self._normalize_thinking_config(effective)
        return normalized or {"type": "disabled"}
