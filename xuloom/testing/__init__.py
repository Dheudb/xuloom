"""Testing and evaluation helpers.

* :class:`ScriptedModel` – a mock model adapter answering from a script.
* :class:`ReplayModel` – replays a run recorded with ``EngineConfig(record_responses=...)``.
* :func:`run_eval` / :func:`load_cases` / ``xuloom eval`` – run tasks in fresh workspaces and check the outcome.

See ``docs/replay-and-eval.md``.
"""
from xuloom.testing.evals import CHECK_TYPES, CaseResult, EvalCase, EvalReport, load_cases, run_eval
from xuloom.testing.models import (ReplayModel, ResponseRecorder, ScriptExhausted, ScriptedModel, load_cassette,
                                   response_from_record)

__all__ = ["ScriptedModel", "ReplayModel", "ResponseRecorder", "ScriptExhausted", "load_cassette",
           "response_from_record", "EvalCase", "CaseResult", "EvalReport", "load_cases", "run_eval", "CHECK_TYPES"]
