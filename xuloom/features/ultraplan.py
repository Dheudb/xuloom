"""
Ultraplan: dedicated deep-planning subsystem.

Spawns an isolated planning sub-agent that focuses exclusively on producing
a comprehensive, actionable plan for complex tasks. The plan is then written
back into the PlanManager for the main agent to execute.
"""

import asyncio
import uuid
import time
from typing import Optional, Callable


ULTRAPLAN_TIMEOUT_S = 30 * 60  # 30 minutes

ULTRAPLAN_INSTRUCTIONS = """\
You are an expert planning agent. Your ONLY job is to produce a thorough,
step-by-step implementation plan for the task described below.

## Rules
1. You must NOT modify any files. You are in **plan-only mode**.
2. You MAY use read-only tools (file_read_tool, grep_tool, glob_tool) to
   explore the codebase and gather context before writing the plan.
3. Use todo_write_tool to track your own planning sub-tasks if needed.
4. When your plan is complete, use plan_tool with action="write" to persist it.
5. Your plan MUST include:
   - A high-level summary (1-3 sentences)
   - Ordered list of implementation steps with file paths
   - Risk assessment and edge cases
   - Testing strategy
6. Be specific about which files to create/modify and what changes to make.
7. After writing the plan, return a brief confirmation message.

## Output format
Write your plan as Markdown inside plan_tool. Structure it as:

```
# Ultraplan: <title>
## Summary
...
## Implementation Steps
1. ...
2. ...
## Risks & Edge Cases
...
## Testing Strategy
...
```
"""


class UltraplanPhase:
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class UltraplanManager:
    """Manages ultraplan lifecycle: launch, monitor, retrieve results."""

    def __init__(self):
        self.phase = UltraplanPhase.IDLE
        self.current_task: Optional[asyncio.Task] = None
        self.last_plan: str = ""
        self.last_error: str = ""
        self.session_id: str = ""
        self.started_at: float = 0
        self.completed_at: float = 0
        self.history: list = []

    async def launch(
        self,
        description: str,
        engine_factory: Callable = None,
        seed_plan: str = "",
        on_phase_change: Optional[Callable[[str], None]] = None,
        runner: Optional[Callable] = None,
    ) -> dict:
        """
        Launch an ultraplan session.

        Args:
            description: The task/feature to plan for.
            engine_factory: Callable that creates a new QueryEngine configured
                            for planning (plan mode, read-only tools, etc.)
            seed_plan: Optional existing plan to refine.
            on_phase_change: Optional callback when phase changes.
        Returns:
            dict with keys: ok, plan, error, elapsed_s
        """
        if self.phase == UltraplanPhase.RUNNING:
            return {"ok": False, "plan": "", "error": "An ultraplan session is already running.", "elapsed_s": 0}

        self.phase = UltraplanPhase.RUNNING
        self.session_id = str(uuid.uuid4())
        self.started_at = time.time()
        self.last_plan = ""
        self.last_error = ""

        if on_phase_change:
            on_phase_change(self.phase)

        try:
            result = await asyncio.wait_for(
                (runner(description, seed_plan) if runner else self._run_planning_agent(description, engine_factory, seed_plan)),
                timeout=ULTRAPLAN_TIMEOUT_S,
            )
            self.phase = UltraplanPhase.COMPLETED
            self.completed_at = time.time()
            elapsed = self.completed_at - self.started_at
            self.last_plan = result
            self.history.append({
                "session_id": self.session_id,
                "description": description[:200],
                "phase": self.phase,
                "elapsed_s": round(elapsed, 1),
            })
            if on_phase_change:
                on_phase_change(self.phase)
            return {"ok": True, "plan": result, "error": "", "elapsed_s": round(elapsed, 1)}

        except asyncio.TimeoutError:
            self.phase = UltraplanPhase.FAILED
            self.last_error = f"Ultraplan timed out after {ULTRAPLAN_TIMEOUT_S}s"
            if on_phase_change:
                on_phase_change(self.phase)
            return {"ok": False, "plan": "", "error": self.last_error, "elapsed_s": ULTRAPLAN_TIMEOUT_S}

        except asyncio.CancelledError:
            self.phase = UltraplanPhase.CANCELLED
            self.last_error = "Ultraplan was cancelled"
            if on_phase_change:
                on_phase_change(self.phase)
            return {"ok": False, "plan": "", "error": self.last_error, "elapsed_s": time.time() - self.started_at}

        except Exception as exc:
            self.phase = UltraplanPhase.FAILED
            self.last_error = str(exc)
            if on_phase_change:
                on_phase_change(self.phase)
            return {"ok": False, "plan": "", "error": self.last_error, "elapsed_s": time.time() - self.started_at}

    async def _run_planning_agent(
        self,
        description: str,
        engine_factory: Callable,
        seed_plan: str = "",
    ) -> str:
        """Spawn a sub-engine in plan mode and run the planning task."""
        sub_engine = engine_factory(
            agent_role="ultraplan",
            forced_mode="plan",
        )

        prompt = self._build_prompt(description, seed_plan)
        result = await sub_engine.run(prompt)
        if not result.ok:
            raise RuntimeError("Planning execution failed: " + result.reason + ": " + result.text)
        plan_content = sub_engine.plan_manager.get_plan()
        if plan_content:
            return plan_content
        return result.text

    def _build_prompt(self, description: str, seed_plan: str = "") -> str:
        parts = [ULTRAPLAN_INSTRUCTIONS]
        if seed_plan:
            parts.append(f"\n## Existing Draft Plan (refine this):\n{seed_plan}")
        parts.append(f"\n## Task Description:\n{description}")
        return "\n".join(parts)

    def cancel(self):
        if self.current_task and not self.current_task.done():
            self.current_task.cancel()
            self.phase = UltraplanPhase.CANCELLED

    def export_state(self) -> dict:
        return {
            "phase": self.phase,
            "session_id": self.session_id,
            "last_plan": self.last_plan,
            "last_error": self.last_error,
            "history": self.history[-10:],
        }

    def load_state(self, state: dict):
        self.phase = state.get("phase", UltraplanPhase.IDLE)
        self.session_id = state.get("session_id", "")
        self.last_plan = state.get("last_plan", "")
        self.last_error = state.get("last_error", "")
        self.history = state.get("history", [])

    def get_status_summary(self) -> str:
        lines = [f"Phase: {self.phase}"]
        if self.session_id:
            lines.append(f"Session: {self.session_id[:8]}...")
        if self.phase == UltraplanPhase.RUNNING:
            elapsed = time.time() - self.started_at
            lines.append(f"Running for: {elapsed:.0f}s")
        if self.last_error:
            lines.append(f"Last error: {self.last_error}")
        if self.history:
            lines.append(f"Total runs: {len(self.history)}")
        return "\n".join(lines)
