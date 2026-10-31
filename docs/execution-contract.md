# Execution and recovery contract

`QueryEngine.run()` returns `RunResult`; `submit_message()` emits the same receipt in the final event's `result`. Callers display `text` and inspect `ok`, `reason`, `persisted` and `execution_reason`. Text is not a completion protocol.

`completed` requires both normal model-loop termination and a successful final checkpoint. Other reasons include `budget_exhausted`, `turn_limit`, `cancelled`, `api_error`, `stream_interrupted`, `tool_input_error`, `tool_protocol_error`, `workflow_incomplete`, `session_busy`, `session_conflict` and `persistence_error`. If the model finishes but the checkpoint fails, `execution_reason` preserves `completed`, while `ok=false` and `reason=persistence_error` prevent a success acknowledgement. Empty delegated results also fail explicitly.

Receipts include `session_id`, `agent_id` and `user_message_id`, populated by code. A delegated receipt's session is its owning conversation. Subagent transcript and parent registry writes must succeed before completion is announced. Failed registry writes restore the previous in-memory row. CLI, desktop, verification and planning consume the same engine receipt.

This is harness completion, not scientific acceptance. A successful receipt does not certify that a paper was read in full, a dependency was satisfied or a scientific result was validated. Domain runtimes retain those checks and their existing job/attempt records. This change adds no second domain scheduler and does not modify private Taiyi skills.

## Execution and permissions

Streaming and batched calls enter `core/tool_execution.py`, which invokes the same `PermissionManager.authorize()` for built-in tools, REPL and MCP. Mode-specific tool visibility remains an additional restriction, not an authorization grant.

Connected MCP tools enter the existing latent catalog in both CLI and desktop. `tool_search_tool` discovers and activates their detailed schemas for subsequent requests; activation does not authorize execution. Explicit role allowlists can load the specified tools directly. Desktop reload replaces connection wrappers while retaining activation for surviving names, and disabling removes both active and latent entries. Registration rejects conflicts with built-in active or latent tools before changing either catalog.

MCP arguments are validated locally against their declared JSON Schema before authorization or remote invocation, including nested constraints and local references. Direct wrapper calls validate as well. Invalid schemas/dialects fail explicitly; schema validation never fetches external reference URLs. Unresolvable references require a self-contained server schema. Validation errors use the existing input-repair receipt and omit argument values.

Plan mode disallows arbitrary REPL, command and MCP execution. Its narrow plan-file exception resolves the real target path. Full Access changes default approvals; it does not override explicit deny/ask rules or Plan restrictions. Matching explicit denies outrank asks, which outrank allows. The desktop bridge preserves mandatory asks even when Full Access is selected while a request is pending.

Before executing a tool, the engine saves its intent in the existing session metadata. After execution it saves the full tool receipt before notifying the UI. Atomic writes retry transient Windows sharing locks for a bounded period; persistent errors remain failures with the previous checkpoint preserved. Current-turn receipts are retained even for batches exceeding the recent-activity preview size. Recovery reuses receipts already saved for the same human turn, tool-call ID and arguments. Reusing an ID with different arguments is rejected.

If a process exits between intent and receipt, effects are uncertain: recovery does not replay the operation. Read-only inspection remains available; a new mutation requires explicit approval, including under Full Access. Cancelling an executing tool preserves the same uncertainty. This is not a claim of transactional exactly-once effects across external systems: an interrupted operation may already have changed them, and must be inspected.

## Engine and desktop shutdown

CLI, desktop and programmatic callers use `QueryEngine.aclose()` for the same resource lifecycle. Concurrent close calls share one cleanup task; cancelling an awaiting caller does not cancel that cleanup. Closing stops active work and owned commands before the final checkpoint, then attempts all model-client, browser, LSP and MCP cleanup. A failed stop, checkpoint or cleanup raises an error and retains the root writer lease. After the cause is resolved, another `aclose()` retries; successful repeated closes do not repeat resource disposal. A child engine closes only its own command group and never releases the parent's lease or shared services.

A closing or closed engine rejects new model turns, authorized tools and worker launches before their side effects. A completed close cannot reacquire a lease by calling `persist_session_state()`. Create a fresh engine to resume. `stop` remains a cancellation operation and does not retire the engine, so the existing continue/edit flow remains available.

The desktop transport requests `shutdown` and waits for `{ "closed": true }` before ending stdin. A shutdown error preserves the application window and unsent draft, reports the failure and permits retry; it is not treated as successful app exit. Normal EOF cleanup uses the same engine close path. Windows acceptance holds a real checkpoint handle without delete sharing to verify the full error/retain/release/retry/restart sequence without repeating a model request.

## Complete tool output

Built-in and MCP outputs use the existing `serialize_tool_result()` and session artifact directory. Oversized content is atomically archived in full. Inline results identify the archive, original size, preview size and truncation, with instructions to read more. Archive failure returns an error and keeps the original content instead of silently discarding it.

MCP text, non-text blocks and structured content are preserved in the complete result. Non-text data is serialized rather than automatically claiming that the selected model can interpret it. `file_read_tool` supports existing line pagination and bounded Unicode `char_offset`/`char_limit` pagination for very long single-line data. This uses the existing file-reading tool rather than a separate MCP paging service.

## Reproducible validation

Run from the repository root:

```powershell
python -m unittest discover -s tests -v
.\.venv-mcp\Scripts\python.exe -m unittest discover -s tests -v
```

The second command is this checkout's Python 3.12 environment with the optional MCP dependency; Python 3.9 runs skip unsupported MCP integration checks. Desktop tests run in isolated profiles with hidden windows. For an installed build, set `XULOOM_TEST_EXECUTABLE` to the stable executable and leave `XULOOM_PYTHON` unset.

| Audit items | Production changes and regression evidence |
| --- | --- |
| 1, 2, 7 | `run_result.py`, engine termination paths and agent transcript checks; `test_execution_contract.py`, `test_subagent_completion.py` |
| 3, 4, 8 | Permission precedence, Plan guard and desktop mandatory-ask handling; `test_permission_modes.py`, `test_execution_contract.py`, desktop permissions E2E |
| 5, 9 | Full MCP output and common archiving; long Unicode text, image and structured-tail markers, archive failure and exact paging round-trip in `test_execution_contract.py` |
| 6 | Extracted engine tool execution and receipts as functions/data; removed unused fork runner and duplicate tool-completion path; no additional manager/state machine |
| 10 | Machine-populated receipt bindings and the domain acceptance boundary documented above |
| 11 | `test_agent_faults.py`: six real worker lifecycles with deterministic model stand-ins, simultaneous start, mixed completion/API error/cancellation, exactly one terminal notification, followup, capacity release and owner shutdown |
| 12 | `test_crash_recovery.py`: actual subprocess exits after durable intent, after side effect and after saved result but before notification; recovery verifies no repeated side effect |
| 13 | `test_provider_contract.py`: actual OpenAI/Anthropic SDKs parse local HTTP/SSE fixtures for parallel calls, fragmented arguments, 429 retry, usage extras and output-limit continuation; lifecycle tests check provider/endpoint inheritance |
| 14 | Python 3.9 and 3.12 full suites, frontend build/unit tests and hidden source/installed Electron E2E; see the local validation record |

These are deterministic framework and protocol tests. They do not establish universal compatibility with every remote gateway or unattended scientific long-run reliability. A1.5 remote validation was explicitly excluded by the user. No private API credential is required by these tests.
