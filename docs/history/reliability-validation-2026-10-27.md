# Local reliability validation — 2026-10-27

Scope: the reliability audit of `v0.1.0-beta.1` (`3a5501cd`), plus the existing local identity-policy changes. The execution contract and item-by-item mapping are in [execution-contract.md](../execution-contract.md). No remote A1.5 test was performed, as requested by the user. No private skill was changed or bundled.

## Environment

Windows 11, hidden Electron windows and disposable test profiles. Python 3.9.7 uses OpenAI SDK 2.15.0, Anthropic SDK 0.86.0 and httpx 0.28.1. Python 3.12.14 and the packaged runtime use OpenAI SDK 3.29.0, Anthropic SDK 1.13.0, httpx 0.28.1 and MCP SDK 1.30.0.

The model protocol tests use actual SDK HTTP/SSE parsing against deterministic local fixtures. MCP tests connect actual local stdio/HTTP servers. Worker lifecycle tests run real framework workers with deterministic model stand-ins. Crash tests exit disposable Python processes with an actual file side effect. This distinguishes tested execution paths from live remote-model evaluation.

## Evidence

- Initial full regression after the structured-return change exposed old string-return test adapters and a Windows newline alteration in archived output. Both were corrected; the intermediate 141-test run passed with one skip.
- Adding execution checkpoints exposed an overbroad cache hit when standalone test calls had no human-turn identity. Cache reuse now requires that identity. The initial 142-test runs with this failure remain in the ignored local build logs.
- Crash testing first exposed incorrect human-turn lookup through tool-result messages. The lookup now excludes tool results; all three process-exit boundaries pass without repeated file effects.
- Concurrency fault testing found duplicate cancellation notifications from nested handlers. After correction, six simultaneous workers produce three completions, one API failure and two cancellations, with one terminal notification each; followup, capacity recovery and owner shutdown pass.
- SDK fixtures initially used the wrong transport type for the newer Anthropic SDK. The fixtures now use that SDK's transport; old and new SDK versions are exercised in their respective Python suites.
- The 153-test full regressions passed on Python 3.9 (five skips) and 3.12 (one skip). Four Python 3.9 skips cover unavailable optional MCP integration; the remaining skip in both interpreters is unavailable symlink creation on this Windows host.
- Frontend TypeScript/Vite build passed. Desktop logic tests passed, 46/46. Vite's existing bundle-size advisory remains.
- Hidden source Electron tests passed for permissions, stopped-turn continuation/editing/restart, MCP market and actual MCP calls, subagent completion barriers, stream interruption/timeouts and tool/workflow recovery.
- A permissions E2E initially failed after a pending approval was released; the test now waits for the actual completion reply and captures UI/request diagnostics. Installed-build diagnostics then exposed a failed tool-result checkpoint after the file write. Windows destination sharing violations now receive bounded retries; persistent failures remain errors and preserve the previous checkpoint. New fault tests cover both outcomes. Storage failures have a distinct `persistence_error` receipt instead of `api_error`.
- An installed stopped-turn edit test initially timed out after restart; the same scenario passes after the storage update. Its original failure log is retained. Transport-level diagnostics were added to the fixture; this is not evidence that every possible UI timing race has been eliminated.

Final verification:

| Check | Result | Ignored local log under `desktop/build/` |
| --- | --- | --- |
| Python 3.9.7 full regression | 156 tests; 151 passed, 5 skipped; 155.075 s | `reliability-python39-release.log` |
| Python 3.12.14 full regression | 156 tests; 155 passed, 1 skipped; 173.087 s | `reliability-python312-release.log` |
| TypeScript/Vite build | Passed | `reliability-frontend-build.log` |
| Desktop unit tests | 46/46 passed | `reliability-desktop-final.log` |
| Packaged Python fault contracts | 16/16 passed | `reliability-installed-contract-storage.log` |
| Original EXE permissions E2E | Passed, including pending release and fail-closed mode-change failure | `reliability-installed-permissions-storage.log` |
| Original EXE stopped-turn resume/edit/restart E2E | Passed | `reliability-installed-resume-storage.log` |
| Original EXE MCP E2E | Passed | `reliability-installed-mcp.log` |
| Original EXE child completion barrier E2E | Passed | `reliability-installed-subagent-completion.log` |
| Original EXE streaming E2E | Passed | `reliability-installed-streaming.log` |
| Original EXE tool/workflow recovery E2E | Passed | `reliability-installed-tool-recovery.log` |

The installed MCP, child barrier, streaming and workflow checks ran against build `20261027-1622`; after the storage correction, build `20261027-1632` was verified with packaged fault contracts, permissions and resume/edit/restart. Earlier failures are not discarded or counted as successful runs. Each E2E uses local fixtures rather than a remote model account.

## Delivery

The stable launch path remains `desktop/release/CodeClaw-win32-x64/CodeClaw.exe`, with final resource build `20261027-1632`. Only application/Python resources are replaced, with rollback resources retained under `desktop/build`. The executable hash remains `2529B494F4123F152BCE346051F7BD855EF95006424A561953CF17C49A3D1BA8`. Only the three public built-in skills are bundled. Local build logs and staging directories are ignored by Git.

These tests do not certify all remote providers, Linux/macOS binaries or unattended scientific end-to-end runs. Harness completion is deliberately separate from domain acceptance. No remote push or release upload is part of this local repair delivery.
