# Working on Xuloom

Guidance for contributors and coding agents. Product documentation starts at [docs/README.md](docs/README.md); the code map is in [docs/architecture.md](docs/architecture.md).

## Layout

- `xuloom/core/` – `QueryEngine` (`engine.py` + mixins in `engine_parts/`), `EngineConfig`, hooks, sessions.
- `xuloom/models/` – `ModelAdapter` and the adapter registry. `xuloom/plugins/` – `PluginAPI` and plugin loading.
- `xuloom/observability/` – spans, exporters, prices and the task cost cap.
- `xuloom/tools/`, `xuloom/agents/`, `xuloom/context/`, `xuloom/features/` – built-in tools, subagents, context and feature managers.
- `xuloom/media/` – the bundled `media` plugin; use it as the reference for features built on `PluginAPI`.
- `desktop/` – Electron + React app talking to `xuloom/desktop/bridge.py` over JSON-lines RPC.

The stable public API is what `xuloom/__init__.py` exports; everything else, including `engine.tool_context`, is internal and may change.

## Rules

- Python 3.10+ for the library and CLI. Keep the existing line endings of each file.
- New capabilities should be reachable through the public extension points (`EngineConfig`, `PluginAPI`, hooks, `ModelAdapter`) rather than special cases in the engine.
- Every bug fix comes with a regression test that fails before the fix. Tests use local fake model servers or `ScriptedAdapter` (`tests/test_model_adapters.py`); never real API keys.
- Before handing off: `ruff check .`, `mypy`, and `python -m unittest discover -s tests`. Behaviour-preserving refactors should also compare `xuloom exec --json` request bodies against the previous version using a fake OpenAI-compatible server.
- Update the matching document in `docs/` and the "未发布" section of `CHANGELOG.md`. Dated validation reports belong in `docs/history/`.
- Do not commit credentials, sessions, virtual environments or build output.

## Desktop update delivery

- Keep one stable user launch path: `desktop/release/Xuloom-win32-x64/Xuloom.exe` on Windows x64.
- Update that installation and its resources. Do not create additional feature-named executables or portable application directories unless the user explicitly requests another build.
- If Xuloom is running, stage only the changed application/Python package resources under `desktop/build`, then use `desktop/scripts/update-existing.ps1` to apply them after that installation exits. Preserve rollback resources. Never terminate the user's app to replace it.
- Run UI checks with isolated test configuration and hidden windows. Source Electron tests are suitable while the installed application is still open; after the update, verify the same installed executable.
