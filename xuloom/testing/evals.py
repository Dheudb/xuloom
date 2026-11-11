"""A minimal eval runner: run tasks in fresh workspaces and check the outcome.

Cases are JSON/JSONL/YAML::

    [{"id": "write-file",
      "prompt": "Create hello.txt containing hi",
      "files": {"README.md": "existing file"},              # optional workspace contents
      "cassette": "cassettes/write-file.jsonl",             # optional: replay instead of a live model
      "checks": [{"type": "ok"},
                 {"type": "file_contains", "path": "hello.txt", "text": "hi"},
                 {"type": "tool_called", "name": "file_write_tool"},
                 {"type": "max_cost_usd", "value": 0.05}]}]

Check types: ``ok``, ``reason`` (``equals``), ``contains`` / ``not_contains`` (``text``,
``ignore_case``) and ``regex`` (``pattern``) on the final answer, ``file_exists`` /
``file_missing`` / ``file_contains`` (``path``, ``text``), ``tool_called`` /
``tool_not_called`` (``name``), ``max_cost_usd`` / ``max_requests`` / ``max_seconds``
(``value``). A case passes when every check passes.
"""
from __future__ import annotations

import json
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

__all__ = ["EvalCase", "CaseResult", "EvalReport", "load_cases", "run_eval", "CHECK_TYPES"]

CHECK_TYPES = frozenset({"ok", "reason", "contains", "not_contains", "regex", "file_exists", "file_missing",
                         "file_contains", "tool_called", "tool_not_called", "max_cost_usd", "max_requests",
                         "max_seconds"})


@dataclass
class EvalCase:
    id: str
    prompt: str
    checks: List[dict] = field(default_factory=list)
    files: Dict[str, str] = field(default_factory=dict)
    cassette: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict, *, base: Optional[Path] = None, index: int = 0) -> "EvalCase":
        if not isinstance(data, dict):
            raise ValueError(f"Case #{index + 1} must be an object")
        unknown = set(data) - {"id", "prompt", "checks", "files", "cassette"}
        if unknown:
            raise ValueError(f"Case #{index + 1}: unknown field(s) " + ", ".join(sorted(unknown)))
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"Case #{index + 1}: 'prompt' is required")
        checks = list(data.get("checks") or [])
        for check in checks:
            if not isinstance(check, dict) or check.get("type") not in CHECK_TYPES:
                raise ValueError(f"Case #{index + 1}: unknown check {check!r}; types: {', '.join(sorted(CHECK_TYPES))}")
        files = data.get("files") or {}
        if not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
            raise ValueError(f"Case #{index + 1}: 'files' maps relative paths to text")
        cassette = data.get("cassette")
        if cassette and base is not None and not Path(cassette).is_absolute():
            cassette = str((base / cassette).resolve())
        return cls(id=str(data.get("id") or f"case-{index + 1}"), prompt=prompt, checks=checks, files=dict(files),
                   cassette=cassette)


@dataclass
class CaseResult:
    id: str
    passed: bool
    reason: str
    text: str
    checks: List[dict]
    seconds: float
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: Optional[float]
    tools: List[str]
    error: Optional[str] = None


@dataclass
class EvalReport:
    cases: List[CaseResult]

    @property
    def passed(self) -> int:
        return sum(1 for case in self.cases if case.passed)

    @property
    def ok(self) -> bool:
        return bool(self.cases) and self.passed == len(self.cases)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "passed": self.passed, "total": len(self.cases),
                "cases": [asdict(case) for case in self.cases]}


def load_cases(path) -> List[EvalCase]:
    """Load cases from ``.json`` (list or ``{"cases": [...]}``), ``.jsonl`` or ``.yaml``/``.yml``."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        raw: Any = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif path.suffix.lower() in {".yaml", ".yml"}:
        import yaml
        raw = yaml.safe_load(text)
    else:
        raw = json.loads(text)
    if isinstance(raw, dict):
        raw = raw.get("cases")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path} must contain a non-empty list of cases")
    cases = [EvalCase.from_dict(item, base=path.parent, index=index) for index, item in enumerate(raw)]
    seen = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"Duplicate case id {case.id!r}")
        seen.add(case.id)
    return cases


def _inside(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"Path {relative!r} leaves the case workspace")
    return target


def _check(check: dict, *, root: Path, text: str, result: dict, tools: List[str], cost: Optional[float],
           requests: int, seconds: float) -> dict:
    kind = check["type"]
    detail = ""
    try:
        if kind == "ok":
            passed = bool(result.get("ok"))
            detail = result.get("reason", "")
        elif kind == "reason":
            passed = result.get("reason") == check.get("equals")
            detail = result.get("reason", "")
        elif kind in {"contains", "not_contains"}:
            needle, haystack = str(check.get("text", "")), text
            if check.get("ignore_case"):
                needle, haystack = needle.lower(), haystack.lower()
            passed = (needle in haystack) == (kind == "contains")
        elif kind == "regex":
            passed = re.search(str(check.get("pattern", "")), text, re.MULTILINE) is not None
        elif kind in {"file_exists", "file_missing"}:
            passed = _inside(root, str(check.get("path", ""))).exists() == (kind == "file_exists")
        elif kind == "file_contains":
            target = _inside(root, str(check.get("path", "")))
            passed = target.is_file() and str(check.get("text", "")) in target.read_text(encoding="utf-8", errors="replace")
        elif kind in {"tool_called", "tool_not_called"}:
            passed = (str(check.get("name", "")) in tools) == (kind == "tool_called")
            detail = ", ".join(tools)
        elif kind == "max_cost_usd":
            passed = cost is not None and cost <= float(check["value"])
            detail = "no price configured" if cost is None else f"${cost:.6f}"
        elif kind == "max_requests":
            passed = requests <= int(check["value"])
            detail = str(requests)
        else:  # max_seconds
            passed = seconds <= float(check["value"])
            detail = f"{seconds:.2f}s"
    except (OSError, ValueError, KeyError, re.error) as error:
        passed, detail = False, f"{type(error).__name__}: {error}"
    return {**check, "passed": bool(passed), "detail": detail}


async def run_case(case: EvalCase, config, *, workspace: Path, permission_handler=None,
                   on_event: Optional[Callable[[str, dict], Any]] = None) -> CaseResult:
    from xuloom.core.engine import QueryEngine
    from xuloom.testing.models import ReplayModel
    for relative, content in case.files.items():
        target = _inside(workspace, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    replay = ReplayModel(case.cassette) if case.cassette else None
    case_config = config.replace(cwd=str(workspace))
    if replay is not None:
        replay.install()
        case_config = replay.config(case_config, collaboration_mode="solo")
    started = time.monotonic()
    events: List[dict] = []
    error = None
    usage = {"input": 0, "output": 0, "requests": 0}
    try:
        async with QueryEngine(case_config, permission_handler=permission_handler) as engine:
            async for event in engine.submit_message(case.prompt, sys_print_callback=lambda *_: None):
                events.append(event)
                if on_event is not None:
                    on_event(case.id, event)
                if event.get("type") == "token_usage":
                    usage["input"] += int(event.get("input_tokens") or 0)
                    usage["output"] += int(event.get("output_tokens") or 0)
                    usage["requests"] += 1
            cost = None if engine.cost_meter.task_unpriced else round(engine.cost_meter.task_usd, 6)
    except Exception as error_:  # a crashing case is a failed case, not a failed eval run
        error, cost = f"{type(error_).__name__}: {error_}", None
    finally:
        if replay is not None:
            replay.uninstall()
    seconds = round(time.monotonic() - started, 3)
    final: dict = next((event for event in reversed(events) if event.get("type") == "final"), {})
    result: dict = final.get("result") or {"ok": False, "reason": "error" if error else "no_result"}
    text = str(final.get("text") or "")
    tools = sorted({str(event.get("tool_name")) for event in events if event.get("type") == "tool_scheduled" and event.get("tool_name")})
    checks = [_check(check, root=workspace, text=text, result=result, tools=tools, cost=cost,
                     requests=usage["requests"], seconds=seconds) for check in case.checks]
    passed = error is None and all(check["passed"] for check in checks)
    return CaseResult(id=case.id, passed=passed, reason=str(result.get("reason", "")), text=text[:4000], checks=checks,
                      seconds=seconds, requests=usage["requests"], input_tokens=usage["input"],
                      output_tokens=usage["output"], cost_usd=cost, tools=tools, error=error)


async def run_eval(cases: Iterable[EvalCase], config, *, workdir=None, permission_handler=None,
                   on_event: Optional[Callable[[str, dict], Any]] = None) -> EvalReport:
    """Run each case in its own empty workspace (under ``workdir`` or a temporary folder).

    ``config`` is the :class:`EngineConfig` used for live cases; ``cwd`` is replaced per case.
    Cases run one after another so their costs and timings stay comparable.
    """
    results = []
    with tempfile.TemporaryDirectory(prefix="xuloom-eval-") as temporary:
        root = Path(workdir).resolve() if workdir else Path(temporary)
        for index, case in enumerate(cases):
            workspace = root / f"{index + 1:03d}-{re.sub(r'[^A-Za-z0-9_.-]+', '-', case.id)[:60]}"
            workspace.mkdir(parents=True, exist_ok=False)
            results.append(await run_case(case, config, workspace=workspace.resolve(),
                                          permission_handler=permission_handler, on_event=on_event))
    return EvalReport(results)
