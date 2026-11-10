import asyncio
import json
import os
import sys
import time
from datetime import datetime
from collections import deque
from contextlib import contextmanager, redirect_stdout

from rich.console import Console, Group
from xuloom.terminal_markdown import TerminalMarkdown, MarkdownStream, terminal_links
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from xuloom.core.config import CONFIG_FILE, load_config_into_env, save_current_env_to_config, load_config
from xuloom.core.model_catalog import connections, select_model, active_connection, apply_connection_env

console = Console()


class InteractiveTerminalTUI:
    """Append stable Markdown blocks without repainting terminal scrollback."""

    HIDE_CURSOR = "\x1b[?25l"
    SHOW_CURSOR = "\x1b[?25h"
    ERASE_LINE  = "\x1b[2K"

    def __init__(self, console: Console, engine_getter):
        self.console = console
        self.engine_getter = engine_getter
        self._stream = sys.stderr if sys.stderr.isatty() else sys.stdout
        self.reset_turn()

    @property
    def engine(self):
        return self.engine_getter()

    def reset_turn(self, user_input: str = ""):
        self.user_input = user_input.strip()
        self.current_turn_text = ""
        self.running = False
        self.turn_number = 0
        self.tool_count = 0
        self.last_tool = ""
        self.last_transition = ""
        self.last_stop_reason = ""
        self.pending_auto_commit = False
        self.replayed = False
        self.event_log = deque(maxlen=10)
        self._status_visible = False
        self._streaming_text = False
        self.compacting = False
        self.compaction_ids = set()
        self.markdown = MarkdownStream(self.console, getattr(self.engine, "session_cwd", os.getcwd()), terminal_links(self.console))

    def _write_status_line(self):
        """Overwrite the current line with a compact status bar."""
        icon = "● RUNNING" if self.running else "○ IDLE"
        mode = self.engine.get_mode()
        parts = [icon, f"Mode: {mode}", f"Turn: {self.turn_number or '-'}",
                 f"Tools: {self.tool_count}"]
        if self.compacting: parts.append("正在压缩上下文…")
        if self.last_tool:
            parts.append(f"Active: {self.last_tool}")
        line = " │ ".join(parts)
        try:
            cols = os.get_terminal_size().columns
        except OSError:
            cols = 100
        line = line[:cols - 1]
        self._stream.write(f"\r{self.ERASE_LINE}{line}")
        self._stream.flush()
        self._status_visible = True

    def _clear_status_line(self):
        if self._status_visible:
            self._stream.write(f"\r{self.ERASE_LINE}\r")
            self._stream.flush()
            self._status_visible = False

    def start_streaming(self):
        self._stream.write(self.HIDE_CURSOR)
        self._stream.flush()
        self._write_status_line()

    def stop_streaming(self):
        self._clear_status_line()
        self.markdown.finish()
        self._stream.write(self.SHOW_CURSOR)
        self._stream.flush()

    def track_stream_event(self, event: dict):
        event_type = event.get("type")
        if event_type == "message_start":
            self.running = True
            self.turn_number = int(event.get("turn", 0) or 0)
            self.current_turn_text = ""
            self.last_stop_reason = ""
            self._write_status_line()
        elif event_type == "text_delta":
            text = event.get("text", "")
            if text:
                self.current_turn_text += text
                self.replayed = bool(event.get("replayed"))
                if not self._streaming_text:
                    self._clear_status_line()
                    self._streaming_text = True
                self.markdown.append(text)
        elif event_type == "context_compaction":
            self._clear_status_line()
            status = event.get("status")
            self.compacting = status == "running"
            self.markdown.finish()
            if self.compacting:
                self.console.print("正在压缩上下文…", markup=False)
            else:
                labels = {"completed": "上下文已自动压缩", "failed": "上下文压缩失败，原始记录已保留", "cancelled": "上下文压缩已取消", "interrupted": "上下文压缩已中断"}
                if event.get("id") not in self.compaction_ids:
                    self.console.print(labels.get(status, "上下文压缩状态变化"), markup=False)
                    self.compaction_ids.add(event.get("id"))
            self._write_status_line()
        elif event_type == "tool_scheduled":
            self.markdown.finish()
            self.tool_count += 1
            self.last_tool = event.get("tool_name", "") or ""
            if self._streaming_text:
                self._stream.write("\n")
                self._streaming_text = False
            self._clear_status_line()
            self.console.print(f"  [dim cyan]⚡ {self.last_tool}[/dim cyan]")
            self._write_status_line()
        elif event_type == "loop_transition":
            self.last_transition = event.get("reason", "") or ""
            if self._streaming_text:
                self._stream.write("\n")
                self._streaming_text = False
            self._clear_status_line()
            self.console.print(f"  [dim]⚡ State transition: {self.last_transition}[/dim]")
            self._write_status_line()
        elif event_type == "auto_commit_proposal":
            self.pending_auto_commit = True
        elif event_type == "message_stop":
            self.markdown.finish()
            self.running = False
            self.last_stop_reason = event.get("stop_reason", "") or ""
            if self._streaming_text:
                self._stream.write("\n")
                self._streaming_text = False
            self._clear_status_line()
            if self.last_stop_reason:
                self.console.print(f"  [dim]⚡ Message stopped: {self.last_stop_reason}[/dim]")

    @contextmanager
    def suspend_streaming(self):
        was_visible = self._status_visible
        self._clear_status_line()
        self._stream.write(self.SHOW_CURSOR)
        self._stream.flush()
        try:
            yield
        finally:
            if was_visible:
                self._stream.write(self.HIDE_CURSOR)
                self._stream.flush()
                self._write_status_line()

    def choose_menu(self, *, title: str, body_lines: list[str], options: list[dict], prompt_label: str, default_value=None):
        with self.suspend_streaming():
            self.console.print()
            self.console.print(
                Panel("\n".join(body_lines), title=title, border_style="yellow", expand=False)
            )
            for index, option in enumerate(options, start=1):
                self.console.print(f"[cyan]{index}[/cyan]. {option['label']}")

            while True:
                raw = self.console.input(
                    f"[bold yellow]{prompt_label}[/bold yellow] [1-{len(options)}]: "
                ).strip().lower()
                if raw == "" and default_value is not None:
                    return default_value
                for index, option in enumerate(options, start=1):
                    aliases = set(str(alias).lower() for alias in option.get("aliases", []))
                    aliases.add(str(option.get("value", "")).lower())
                    aliases.add(str(index))
                    if raw in aliases:
                        return option.get("value")
                self.console.print("[red]Invalid selection.[/red] Please enter a valid number.")



def print_welcome_screen():
    console.print()
    
    art_lines = [
        "  \u2584\u2588\u2588\u2584              \u2584\u2588\u2588\u2584  ",
        "  \u2588\u2588\u2588\u2588  \u2584\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2584  \u2588\u2588\u2588\u2588  ",
        "  \u2588\u2588\u2588\u2588 \u2588  [bold cyan]\u2588\u2588[/]  [bold cyan]\u2588\u2588[/]  \u2588 \u2588\u2588\u2588\u2588  ",
        "   \u2580\u2588\u2588 \u2588 [#f08080]\u2592\u2592[/] vv [#f08080]\u2592\u2592[/] \u2588 \u2588\u2588\u2580   ",
        "   \u2580\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2580           ",
    ]
    bot_art = Text()
    for line in art_lines:
        bot_art.append_text(Text.from_markup(line, style="white"))
        bot_art.append("\n")
    bot_art.append("   Xuloom               ", style="bold deep_sky_blue1")

    display_provider = os.environ.get("XULOOM_MODEL_PROVIDER", "").strip() or ""
    display_model = ""
    if display_provider == "openai":
        display_model = os.environ.get("OPENAI_MODEL", "")
    elif display_provider == "anthropic":
        display_model = os.environ.get("ANTHROPIC_MODEL", "")
    if not display_provider:
        display_model = os.environ.get("ANTHROPIC_MODEL", "") or os.environ.get("OPENAI_MODEL", "")

    model_str = f"[cyan]{display_model}[/cyan]" if display_model else "[dim](not set — use /model)[/dim]"
    provider_str = f"[green]{display_provider}[/green]" if display_provider else "[dim](not set — use /model)[/dim]"

    info_table = Table.grid(padding=(0, 2))
    info_table.add_column()
    info_table.add_row("[bold white]Welcome to Xuloom CLI![/bold white]")
    info_table.add_row("[dim]Send /help or /model to configure.[/dim]\n")
    info_table.add_row(f"[dim]Model:[/dim]    {model_str}")
    info_table.add_row(f"[dim]Provider:[/dim] {provider_str}\n")

    main_grid = Table.grid(padding=(1, 4))
    main_grid.add_column(justify="center", vertical="top")
    main_grid.add_column(justify="left", vertical="top")
    main_grid.add_row(bot_art, info_table)

    lower_table = Table.grid(padding=(0, 4, 0, 0))
    lower_table.add_column(justify="left", min_width=15)
    lower_table.add_column(justify="left", min_width=12)
    lower_table.add_column(justify="left", min_width=10)
    lower_table.add_column(justify="left")

    lower_table.add_row(
        "[bold white]Protocols[/bold white]",
        "[bold white]Shortcuts[/bold white]",
        "[bold white]Status[/bold white]",
        "[bold white]Date & Time[/bold white]"
    )
    lower_table.add_row("", "", "", "")

    def _p_indicator(name):
        if display_provider and name.startswith(display_provider):
            return f"[green]● {name}[/green]"
        return f"[dim]○ {name}[/dim]"

    has_key = bool(
        os.environ.get("ANTHROPIC_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    ) if display_provider else False
    status_str = "[green]● Ready[/green]" if (display_provider and display_model and has_key) else "[yellow]● Not configured[/yellow]"

    lower_table.add_row(
        _p_indicator("anthropic"),
        "[cyan]/palette[/cyan]",
        status_str,
        f"[cyan]{datetime.now().strftime('%b %d')}[/cyan]",
    )
    lower_table.add_row(
        _p_indicator("openai"),
        "[cyan]/model[/cyan]",
        "",
        f"[cyan]{datetime.now().strftime('%I:%M %p')}[/cyan]",
    )
    lower_table.add_row(
        _p_indicator("local"),
        "[cyan]/quit[/cyan]",
        "",
        "",
    )

    content = Group(
        main_grid,
        "\n",
        lower_table
    )
    
    panel = Panel(
        content,
        border_style="deep_sky_blue1",
        expand=False,
        padding=(1, 2)
    )
    console.print(panel)
    console.print()


def run_command_palette(tui: InteractiveTerminalTUI) -> str:
    choice = tui.choose_menu(
        title="Command Palette",
        body_lines=[
            "Select a built-in command.",
            "Press Enter to cancel and return to input mode.",
        ],
        options=[
            {"value": "/help", "label": "Show help"},
            {"value": "/status", "label": "View runtime status"},
            {"value": "/git", "label": "View repository and branch"},
            {"value": "/summary", "label": "View saved output files and sources"},
            {"value": "/summary all", "label": "View outputs and sources for the whole conversation"},
            {"value": "/history", "label": "Read saved dialogue; /history agent-id shows a registered child"},
            {"value": "/link", "label": "Show this chat's local deep link"},
            {"value": "/tools", "label": "View tool activity"},
            {"value": "/todos", "label": "View structured todos"},
            {"value": "/mode", "label": "View current mode"},
            {"value": "/plan", "label": "Enable plan mode"},
            {"value": "/plan off", "label": "Disable plan mode"},
            {"value": "/coordinator", "label": "Enable coordinator mode"},
            {"value": "/coordinator off", "label": "Disable coordinator mode"},
            {"value": "/team", "label": "View team status"},
            {"value": "/model", "label": "Switch model/provider"},
            {"value": "/model status", "label": "Show current model runtime"},
            {"value": "/thinking", "label": "Show model Thinking and effort settings"},
            {"value": "/sessions", "label": "List session history"},
            {"value": "/resume", "label": "Resume a session"},
            {"value": "/quit", "label": "Quit CLI"},
        ],
        prompt_label="Command",
        default_value="",
    )
    if choice == "/resume":
        with tui.suspend_streaming():
            sid = console.input("[bold yellow]Session ID[/bold yellow]: ").strip()
        return f"/resume {sid}" if sid else ""
    return choice or ""


PROVIDER_PRESETS = {
    "anthropic": {
        "label": "Anthropic protocol",
        "key_env": "ANTHROPIC_API_KEY",
        "base_url_env": "ANTHROPIC_BASE_URL",
        "model_env": "ANTHROPIC_MODEL",
        "effective_provider": "anthropic",
    },
    "openai": {
        "label": "OpenAI protocol",
        "key_env": "OPENAI_API_KEY",
        "base_url_env": "OPENAI_BASE_URL",
        "model_env": "OPENAI_MODEL",
        "effective_provider": "openai",
    },
    "local": {
        "label": "Local (OpenAI-compatible)",
        "key_env": "OPENAI_API_KEY",
        "base_url_env": "OPENAI_BASE_URL",
        "model_env": "OPENAI_MODEL",
        "effective_provider": "openai",
    },
}


async def _interactive_model_switch(engine, tui):
    """Interactive wizard to switch protocol, model, API base URL, and API key."""

    current_model = engine.primary_model or "(empty)"
    current_provider = engine.model_provider or "(empty)"
    current_base = engine.api_base_url or os.environ.get("ANTHROPIC_BASE_URL", "") or "(empty)"

    provider_choice = tui.choose_menu(
        title="Configure Model",
        body_lines=[
            f"Current protocol: [cyan]{current_provider}[/cyan]",
            f"Current model:    [cyan]{current_model}[/cyan]",
            f"Current base URL: [dim]{current_base}[/dim]",
            "",
            "Select protocol:",
        ],
        options=[
            {"value": "anthropic", "label": "Anthropic protocol"},
            {"value": "openai",    "label": "OpenAI protocol"},
            {"value": "local",     "label": "Local (OpenAI-compatible)"},
            {"value": "", "label": "Cancel"},
        ],
        prompt_label="Protocol",
        default_value="",
    )
    if not provider_choice:
        console.print("[dim]Cancelled.[/dim]")
        return

    preset = PROVIDER_PRESETS.get(provider_choice, {})
    effective_provider = preset.get("effective_provider", provider_choice)
    base_url_env = preset.get("base_url_env", "")
    key_env = preset.get("key_env", "")
    model_env = preset.get("model_env", "")

    with tui.suspend_streaming():
        model_input = console.input(
            f"[bold yellow]Model name[/bold yellow] [dim](current: {current_model}, Enter to keep)[/dim]: "
        ).strip()

    with tui.suspend_streaming():
        url_input = console.input(
            f"[bold yellow]API Base URL[/bold yellow] [dim](current: {current_base}, Enter to keep)[/dim]: "
        ).strip()

    current_key_set = bool(os.environ.get(key_env, ""))
    key_hint = "set" if current_key_set else "not set"
    with tui.suspend_streaming():
        key_input = console.input(
            f"[bold yellow]API Key ({key_env})[/bold yellow] [dim](currently {key_hint}, Enter to keep)[/dim]: "
        ).strip()

    if url_input and base_url_env:
        os.environ[base_url_env] = url_input
    if model_input and model_env:
        os.environ[model_env] = model_input
    os.environ["XULOOM_MODEL_PROVIDER"] = effective_provider

    result = engine.switch_model_runtime(
        provider=effective_provider,
        model=model_input or None,
        api_base_url=url_input if effective_provider == "openai" else None,
        api_key=key_input or None,
    )

    info = Table(title="Model Runtime Updated", border_style="green", expand=False)
    info.add_column("Setting", style="bold")
    info.add_column("Value", style="cyan")
    info.add_row("Provider", result["provider"])
    info.add_row("Model", result["model"])
    info.add_row("API Base URL", result["api_base_url"])
    info.add_row("API Key", "✓ set" if result["has_api_key"] else "✗ not set")
    console.print(info)

    save_current_env_to_config()
    console.print(f"[dim]Configuration saved to {CONFIG_FILE}[/dim]")


async def interactive_loop(resume_session_id: str = None, initial_prompt: str = None, auto_approve: bool = False, collaboration_mode=None, execution_mode=None, working_directory=None, strict_resume=False):
    with redirect_stdout(sys.stderr):
        from xuloom.core.engine import QueryEngine
    load_config_into_env()
    print_welcome_screen()

    engine = None
    tui = InteractiveTerminalTUI(console, lambda: engine)

    def permission_prompt(request):
        if auto_approve:
            console.print(f"[dim]Auto-approved: {request.tool_name}[/dim]")
            return "y"
        decision = tui.choose_menu(
            title="Permission Required",
            body_lines=[
                f"Tool: {request.tool_name}",
                f"Risk: {request.risk_level}",
                "",
                request.summary,
            ],
            options=[
                {"value": "y", "label": "Allow once", "aliases": ["allow", "once", "y"]},
                {"value": "a", "label": "Always allow this tool in current session", "aliases": ["always", "a"]},
                {"value": "n", "label": "Deny", "aliases": ["deny", "n"]},
            ],
            prompt_label="授权选择",
            default_value="n",
        )
        console.print(f"权限决策: {request.tool_name} -> {decision}")
        return decision

    async def prompt_auto_commit(proposal: dict):
        if not proposal:
            return
        files = proposal.get("files", []) or []
        lines = [
            f"Branch: {proposal.get('branch') or '<detached>'}",
            f"Repo: {proposal.get('repo_root') or '<unknown>'}",
            f"Message source: {proposal.get('message_source') or 'unknown'}",
            "",
            "Commit message:",
            proposal.get("message") or "<empty>",
            "",
            "Files:",
        ]
        for item in files[:12]:
            lines.append(f"- {item.get('relative_path')} [{item.get('operation')}]")
        if proposal.get("skipped_dirty_overlap_count"):
            lines.extend([
                "",
                f"Skipped pre-existing dirty files: {proposal.get('skipped_dirty_overlap_count')}",
            ])

        decision = tui.choose_menu(
            title="Auto-Commit Proposal",
            body_lines=lines,
            options=[
                {"value": "commit", "label": "Commit now", "aliases": ["y", "yes", "commit"]},
                {"value": "skip", "label": "Skip this proposal", "aliases": ["n", "no", "skip"]},
            ],
            prompt_label="提交选择",
            default_value="commit",
        )
        result = await engine.resolve_auto_commit_proposal(
            proposal.get("proposal_id", ""),
            decision=decision,
        )
        if result.get("ok"):
            console.print(f"[green]{result.get('content')}[/green]")
            console.print(result.get("content", "Auto-commit handled."))
        else:
            console.print(f"[red]{result.get('content')}[/red]")
            console.print(result.get("content", "Auto-commit failed."))
        tui.pending_auto_commit = False

    from xuloom.security.permissions import ApprovalPolicy
    engine = QueryEngine(permission_handler=permission_prompt, approval_policy=ApprovalPolicy("full" if auto_approve else "ask"), execution_mode=execution_mode, cwd=working_directory,
                         load_enabled_plugins=True)

    def require_idle_session():
        if (any(not task.done() for task in engine.tool_context["subagent_tasks"].tasks.values())
            or any(row.get("status") == "running" for row in engine.tool_context["command_sessions"].records.values())
            or any(row.get("status") == "running" for row in engine.shell_task_manager.list_tasks())):
            raise ValueError("Finish or cancel owned workers and commands before replacing or clearing the session")

    async def fresh_session():
        nonlocal engine
        policy, cwd, collaboration, isolation = engine.permission_manager.approval_policy, engine.session_cwd, engine.collaboration_mode, engine.execution_sandbox.mode
        await engine.aclose()
        engine = QueryEngine(permission_handler=permission_prompt, approval_policy=policy, cwd=cwd, collaboration_mode=collaboration, execution_mode=isolation,
                             load_enabled_plugins=True)
        await engine.start_protocols(sys_print=agent_events_callback)

    if not engine.is_configured:
        console.print(
            "[bold yellow]Warning:[/bold yellow] Model not configured. "
            "Use [cyan]/model[/cyan] to set protocol, model name, base URL, and API key.\n"
        )
    else:
        provider_key_name = "OPENAI_API_KEY" if engine.model_provider == "openai" else "ANTHROPIC_API_KEY"
        api_key = os.environ.get(provider_key_name)
        if not api_key:
            console.print(f"[bold yellow]Warning:[/bold yellow] {provider_key_name} is not set in environment.")
            console.print("Use [cyan]/model[/cyan] to configure, or set the environment variable.\n")

    def agent_events_callback(text: str):
        if text.startswith("上下文已压缩：") and tui.compaction_ids: return
        console.print(text)

    # Auto-resume if requested via CLI flag
    if resume_session_id:
        if resume_session_id == "__latest__":
            ids = engine.session_manager.list_session_ids()
            if ids:
                resume_session_id = ids[0]
            else:
                console.print("[dim]No previous sessions found. Starting fresh.[/dim]")
                resume_session_id = None
        if resume_session_id and resume_session_id != "__latest__":
            if engine.load_session(resume_session_id):
                console.print(
                    Panel(
                        engine.get_resume_summary(),
                        title=f"Session '{resume_session_id[:8]}...' resumed",
                        border_style="green",
                        expand=False,
                    )
                )
            else:
                if strict_resume:
                    await engine.aclose()
                    raise ValueError("链接指向的会话无法恢复；未创建替代会话")
                console.print(f"[red]Failed to resume session '{resume_session_id}'. Starting fresh.[/red]")

    if collaboration_mode is not None:
        engine.set_collaboration_mode(collaboration_mode)
    if execution_mode is not None:
        engine.execution_sandbox.mode = execution_mode
        engine.persist_session_state()
    with console.status("[bold cyan]Bootstrapping Advanced Protocols...", spinner="dots"):
        await engine.start_protocols(sys_print=agent_events_callback)

    last_interrupt_time = 0
    _pending_initial_prompt = initial_prompt
    terminal_input = None
    if sys.stdin.isatty() and sys.stdout.isatty():
        from xuloom.skills.terminal import input_session
        terminal_input = input_session(lambda: engine.tool_context["skill_registry"])
    while True:
        try:
            if _pending_initial_prompt:
                user_input = _pending_initial_prompt
                _pending_initial_prompt = None
                console.print(f"\n[bold green]❯[/bold green] [dim]{user_input[:120]}{'...' if len(user_input) > 120 else ''}[/dim]")
            else:
                user_input = await terminal_input.prompt_async("❯ ") if terminal_input else console.input("\n[bold green]❯[/bold green] ")
            last_interrupt_time = 0
            if not user_input.strip():
                continue

            if user_input.strip().lower() in {"/palette", "/menu", "/commands"}:
                selected = run_command_palette(tui)
                if not selected:
                    continue
                user_input = selected

            normalized_input = user_input.strip().lower()

            if normalized_input in {"/quit", "/q", "/exit"}:
                console.print("[dim]Session terminated.[/dim]")
                break
            elif normalized_input == "/help":
                help_text = (
                    "[bold white]Session[/bold white]\n"
                    "  /new              Start a fresh session\n"
                    "  /sessions         List saved sessions\n"
                    "  /resume <id>      Resume a saved session\n"
                    "  /undo <revision>  Restore one file-tool write if it has not changed\n"
                    "  /delete <id>      Delete a saved session\n"
                    "  /clear sessions   Delete all saved sessions\n\n"
                    "[bold white]Planning[/bold white]\n"
                    "  /plan             Enable plan mode\n"
                    "  /plan off         Disable plan mode\n"
                    "  /plan show        Show current plan\n"
                    "  /plan clear       Clear current plan\n"
                    "  /ultraplan <desc> Deep planning via dedicated sub-agent\n\n"
                    "[bold white]Multi-Agent[/bold white]\n"
                    "  /team             View team status\n"
                    "  /coordinator      Toggle coordinator mode\n"
                    "  /coordinator off  Disable coordinator mode\n\n"
                    "[bold white]Inspect[/bold white]\n"
                    "  /mode             View current mode\n"
                    "  /todos            View structured todos\n"
                    "  /tools            View tool activity\n"
                    "  /agents           View subagent registry\n"
                    "  /sandbox          View sandbox status\n"
                    "  /status           View runtime status\n"
                    "  /git              View repository and branch\n\n"
                    "  /summary [all]    View saved outputs, sources and tool counts\n"
                    "  /history [agent-id|-] [cursor]    Read a saved dialogue page without running it\n"
                    "  /link             Show this chat's local deep link\n\n"
                    "[bold white]Model & Provider[/bold white]\n"
                    "  /model            Interactive model/provider switch\n"
                    "  /model status     Show current model runtime\n"
                    "  /model list       List saved API models\n"
                    "  /model use 2      Switch to a saved model\n"
                    "  /thinking         Show Thinking settings; /thinking enabled|disabled|default\n"
                    "  /thinking effort high   Set model effort independently of collaboration\n\n"
                    "[bold white]Other[/bold white]\n"
                    "  /palette          Open command palette\n"
                    "  /quit             Exit CLI\n"
                )
                console.print(Panel(help_text, title="Commands", border_style="cyan", expand=False))
                continue
            elif normalized_input == "/new":
                require_idle_session()
                if engine.session_manager.owns_writer(engine.session_id) and not engine.persist_session_state():
                    raise OSError(engine.session_manager.last_save_error)
                old_sid = engine.session_id
                await fresh_session()
                console.print(f"[bold green]New session started.[/bold green] Previous session: [dim]{old_sid[:8]}...[/dim]")
                continue
            elif normalized_input == "/sessions":
                console.print(engine.session_manager.get_recent_sessions())
                continue
            elif normalized_input.startswith("/delete "):
                parts = user_input.strip().split(maxsplit=1)
                if len(parts) > 1:
                    target_sid = parts[1].strip()
                    if target_sid == engine.session_id:
                        console.print("[red]Cannot delete the active session. Use /new first.[/red]")
                    elif engine.session_manager.delete_session(target_sid):
                        console.print(f"[green]Session [bold]{target_sid[:8]}...[/bold] deleted.[/green]")
                    else:
                        console.print(f"[red]Session '{target_sid}' not found.[/red]")
                continue
            elif normalized_input == "/clear sessions":
                confirm = console.input("[bold yellow]Delete ALL saved sessions? (y/N): [/bold yellow]").strip().lower()
                if confirm == "y":
                    require_idle_session()
                    count = engine.session_manager.clear_all_sessions()
                    await fresh_session()
                    console.print(f"[green]Cleared {count} saved session(s).[/green]")
                else:
                    console.print("[dim]Cancelled.[/dim]")
                continue
            elif normalized_input.startswith("/resume "):
                items = user_input.split(" ")
                if len(items) > 1:
                    sid = items[1]
                    if engine.load_session(sid):
                        console.print(
                            Panel(
                                engine.get_resume_summary(),
                                title=f"Session '{sid[:8]}...' recovered",
                                border_style="green",
                                expand=False,
                            )
                        )
                        pending_auto_commit = engine.get_pending_auto_commit_proposal()
                        if pending_auto_commit:
                            await prompt_auto_commit(pending_auto_commit)
                    else:
                        console.print(f"[red]Error: Failed to resume session '{sid}'.[/red]")
                continue
            elif normalized_input.startswith("/plan"):
                raw = user_input.strip()
                parts = raw.split(maxsplit=1)
                arg = parts[1].strip().lower() if len(parts) > 1 else ""

                if arg in {"", "on"}:
                    engine.set_mode("plan")
                    engine.persist_session_state()
                    console.print("[bold green]Plan mode enabled.[/bold green] Mutating workspace tools will now be blocked.")
                elif arg in {"off", "exit", "normal"}:
                    engine.set_mode("normal")
                    engine.persist_session_state()
                    console.print("[bold green]Returned to normal mode.[/bold green]")
                elif arg in {"show", "status"}:
                    current_plan = engine.get_plan() or "[dim]Current plan is empty.[/dim]"
                    console.print(Panel(current_plan, title=f"Plan Mode: {engine.get_mode()}", border_style="cyan", expand=False))
                elif arg == "clear":
                    engine.plan_manager.clear_plan()
                    engine.persist_session_state()
                    console.print("[bold green]Current plan cleared.[/bold green]")
                else:
                    console.print("[red]Unknown /plan argument.[/red] Use: /plan, /plan off, /plan show, /plan clear")
                continue
            elif normalized_input == "/mode":
                console.print(Panel(engine.get_mode_summary(), title="Session Mode", border_style="cyan", expand=False))
                continue
            elif normalized_input == "/todos":
                console.print(Panel(engine.get_todo_summary(), title="Structured Todos", border_style="cyan", expand=False))
                continue
            elif normalized_input == "/sandbox":
                console.print(Panel(engine.get_sandbox_summary(), title="Sandbox Status", border_style="cyan", expand=False))
                continue
            elif normalized_input == "/agents":
                console.print(Panel(engine.get_agents_summary(), title="Subagent Registry", border_style="cyan", expand=False))
                continue
            elif normalized_input == "/tools":
                console.print(Panel(engine.get_tools_summary(), title="Tool Activity", border_style="cyan", expand=False))
                continue
            elif normalized_input == "/link":
                from xuloom.features.chat_links import chat_link, resolve_link
                url = chat_link(engine.session_cwd, engine.session_id)
                resolve_link(url)
                console.print(url, markup=False)
                continue
            elif normalized_input in {"/git", "/git-status"}:
                from xuloom.features.workspace_files import git_status
                console.print_json(data=await git_status(engine.session_cwd))
                continue
            elif normalized_input in {"/summary", "/summary all"}:
                from xuloom.features.session_summary import read_summary
                console.print_json(data=read_summary(engine.session_cwd, engine.session_id, all_turns=normalized_input.endswith(" all")))
                continue
            elif normalized_input == "/history" or normalized_input.startswith("/history "):
                from xuloom.features.agent_history import inspect, command_params
                values = command_params(user_input.strip().partition(" ")[2])
                console.print_json(data=await inspect(engine.session_cwd, engine.session_id,
                    agent_id=values['agentId'], cursor=values['cursor']))
                continue
            elif normalized_input == "/context" or normalized_input.startswith("/compact"):
                from xuloom.context.compaction import status
                if normalized_input.startswith("/compact"):
                    focus = user_input.strip().partition(" ")[2]
                    try:
                        engine.abort_event.clear()
                        await engine._apply_layered_compaction_if_needed(lambda text: console.print(text, markup=False), force=True, focus=focus)
                    except Exception as error: console.print(str(error), markup=False)
                console.print_json(data=status(engine))
                continue
            elif normalized_input == "/status":
                console.print(Panel(engine.get_runtime_summary(), title="Runtime Status", border_style="cyan", expand=False))
                continue
            elif normalized_input.startswith("/ultraplan"):
                raw = user_input.strip()
                parts = raw.split(maxsplit=1)
                description = parts[1].strip() if len(parts) > 1 else ""
                if not description:
                    console.print("[yellow]Usage:[/yellow] /ultraplan <task description>")
                    console.print("[dim]Launches a dedicated planning sub-agent that produces a comprehensive plan.[/dim]")
                    continue
                seed_plan = engine.get_plan() or ""
                console.print(f"[bold cyan]Launching Ultraplan...[/bold cyan] [dim]{description[:80]}{'...' if len(description) > 80 else ''}[/dim]")
                with console.status("[bold cyan]Ultraplan running — deep analysis in progress...", spinner="dots"):
                    result = await engine.launch_ultraplan(
                        description=description,
                        seed_plan=seed_plan,
                    )
                if result.get("ok"):
                    console.print(Panel(
                        result.get("plan", "")[:3000] or "[dim]Empty plan returned.[/dim]",
                        title=f"[bold green]Ultraplan Complete[/bold green] ({result.get('elapsed_s', 0):.0f}s)",
                        border_style="green",
                        expand=False,
                    ))
                    console.print("[dim]Plan has been written to session. Use /plan show to view full plan.[/dim]")
                else:
                    console.print(f"[bold red]Ultraplan failed:[/bold red] {result.get('error', 'Unknown error')}")
                continue
            elif normalized_input == "/team":
                console.print(Panel(engine.get_team_summary(), title="Team Status", border_style="cyan", expand=False))
                continue
            elif normalized_input.startswith("/coordinator"):
                raw = user_input.strip()
                parts = raw.split(maxsplit=1)
                arg = parts[1].strip().lower() if len(parts) > 1 else ""
                if arg in {"off", "disable"}:
                    engine.disable_coordinator_mode()
                    engine.persist_session_state()
                    console.print("[bold green]Coordinator mode disabled.[/bold green] Full tool set restored.")
                else:
                    engine.enable_coordinator_mode()
                    engine.persist_session_state()
                    console.print(
                        "[bold green]Coordinator mode enabled.[/bold green]\n"
                        "[dim]Tool set restricted to orchestration tools only.\n"
                        "Use agent_tool to spawn workers, send_message_tool to coordinate.\n"
                        "Type /coordinator off to restore full tool access.[/dim]"
                    )
                continue
            elif normalized_input.startswith("/undo "):
                try:
                    console.print(json.dumps(await engine.undo_file(user_input.split(maxsplit=1)[1].strip()), ensure_ascii=False))
                except Exception as error:
                    console.print("Undo refused: " + str(error))
                continue
            elif normalized_input == "/thinking" or normalized_input.startswith("/thinking "):
                parts = user_input.strip().split()
                if len(parts) == 1:
                    console.print(json.dumps(engine.reasoning_profile or {"effort": engine.reasoning_effort or "default"}, ensure_ascii=False))
                    console.print("[dim]/thinking enabled|disabled|default   /thinking effort low|medium|high|xhigh|max|default[/dim]")
                else:
                    try:
                        if len(parts) == 2:
                            result = engine.set_reasoning_options(thinking=parts[1])
                        elif len(parts) == 3 and parts[1] == "effort":
                            result = engine.set_reasoning_options(effort=parts[2])
                        else:
                            raise ValueError("Use /thinking enabled|disabled|default or /thinking effort <level>")
                        if not engine.persist_session_state(): raise OSError("Thinking settings could not be saved")
                        console.print(json.dumps(result, ensure_ascii=False))
                    except (ValueError, OSError, RuntimeError) as error:
                        console.print("Thinking settings: " + str(error))
                continue
            elif normalized_input.startswith("/model"):
                raw = user_input.strip()
                parts = raw.split(maxsplit=1)
                arg = parts[1].strip().lower() if len(parts) > 1 else ""

                if arg == "status":
                    info = Table(title="Model Runtime", border_style="cyan", expand=False)
                    info.add_column("Setting", style="bold")
                    info.add_column("Value", style="cyan")
                    info.add_row("Provider", engine.model_provider)
                    info.add_row("Model", engine.primary_model)
                    info.add_row("Fallback", engine.fallback_model or "(same)")
                    info.add_row("API Base URL", engine.api_base_url or "(default)")
                    key_name = "OPENAI_API_KEY" if engine.model_provider == "openai" else "ANTHROPIC_API_KEY"
                    info.add_row("API Key", "✓ set" if os.environ.get(key_name) else "✗ not set")
                    console.print(info)
                elif arg == "list" or arg.startswith("use "):
                    cfg = load_config()
                    choices = [(connection, model) for connection in connections(cfg) for model in connection.get("models", [])]
                    if arg == "list":
                        table = Table(title="Saved API Models", border_style="cyan")
                        for heading in ("#", "API", "Model"):
                            table.add_column(heading)
                        for index, (connection, model) in enumerate(choices, 1):
                            table.add_row(str(index), connection.get("name", connection["id"]), model)
                        console.print(table)
                        console.print("[dim]Use /model use <number> to switch; /model configures an API.[/dim]")
                    else:
                        number = raw.split(maxsplit=2)[2].strip()
                        if not number.isdigit() or not 1 <= int(number) <= len(choices):
                            console.print("[red]Choose a valid number from /model list.[/red]")
                            continue
                        if any(not task.done() for task in engine.tool_context["subagent_tasks"].tasks.values()):
                            console.print("[yellow]Wait for background agents or stop them before switching.[/yellow]")
                            continue
                        connection, model = choices[int(number) - 1]
                        from xuloom.core.config import update_config
                        cfg = await asyncio.to_thread(update_config, lambda latest: select_model(latest, connection["id"], model))
                        selected = active_connection(cfg)
                        apply_connection_env(selected)
                        previous_client, previous_compactor = engine.client, engine.compactor
                        engine.switch_model_runtime(provider=selected["provider"], model=model,
                            api_base_url=selected.get("base_url", ""), api_key=selected.get("api_key", ""), network_mode=selected.get("network_mode", "direct"))
                        if previous_client is not None:
                            await previous_client.close()
                        if previous_compactor.client:
                            await previous_compactor.client.close()
                        console.print(f"[green]Model switched to {model}.[/green]")
                else:
                    await _interactive_model_switch(engine, tui)
                continue

            final_answer = ""
            turn_was_aborted = False
            tui.reset_turn(user_input)
            tui.start_streaming()

            try:
                async for event in engine.submit_message(user_input, sys_print_callback=agent_events_callback):
                    tui.track_stream_event(event)
                    event_type = event.get("type")
                    if event_type == "auto_commit_proposal":
                        with tui.suspend_streaming():
                            await prompt_auto_commit(event.get("proposal") or {})
                    elif event_type == "final":
                        final_answer = event.get("text", "")
                        receipt = event.get("result", {})
                        if not receipt.get("ok", False):
                            final_answer += "\n[Execution ended: " + receipt.get("reason", "unknown") + "]"
            except (KeyboardInterrupt, asyncio.CancelledError):
                last_interrupt_time = 0
                try:
                    await engine.request_abort()
                except Exception:
                    pass
                turn_was_aborted = True
            finally:
                tui.stop_streaming()

            # Show whatever content was received (partial on abort, full on completion)
            display_text = final_answer or tui.current_turn_text.strip()
            if turn_was_aborted:
                if display_text and display_text.strip() != tui.current_turn_text.strip():
                    console.print(Panel(
                        TerminalMarkdown(display_text, engine.session_cwd, links=terminal_links(console)),
                        title="[yellow]⚡ Interrupted[/yellow]",
                        border_style="yellow",
                        expand=False,
                    ))
                console.print("[dim]Current turn aborted. You can continue or start a new prompt.[/dim]")
                continue

            if display_text and display_text.strip() != tui.current_turn_text.strip():
                console.print(Panel(TerminalMarkdown(display_text, engine.session_cwd, links=terminal_links(console)), border_style="green", expand=False))

        except KeyboardInterrupt:
            current_time = time.time()
            if current_time - last_interrupt_time < 2.0:
                console.print("\n[dim]Received interrupt. Goodbye![/dim]")
                break
            else:
                last_interrupt_time = current_time
                console.print("\n[dim](Press Ctrl+C again within 2 seconds to exit)[/dim]")
                continue
        except asyncio.CancelledError:
            continue
        except Exception as e:
            console.print(f"\n[bold red]Critical Error:[/bold red] {e}")
        except BaseException as e:
            console.print(f"\n[bold red]Fatal System Error:[/bold red] {type(e).__name__}")
            break


    await engine.aclose()


def main():
    # The bundled isolated interpreter ignores PYTHONIOENCODING/PYTHONUTF8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"): stream.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] in {"exec", "goal", "schedule", "pipeline", "plugins", "git-status", "chat-link", "summary", "transcript", "media", "remote", "sandbox-cleanup", "credentials", "mcp", "search-config", "context", "scaling"}:
        from xuloom.cli import main as noninteractive
        raise SystemExit(noninteractive(sys.argv[1:]))
    import argparse
    parser = argparse.ArgumentParser(prog="xuloom", description="Xuloom Agentic CLI Engine")
    parser.add_argument("-c", "--continue", dest="continue_session", action="store_true",
                        help="Resume the most recent session")
    parser.add_argument("-r", "--resume", dest="resume_id", type=str, default=None,
                        help="Resume a specific session by ID")
    parser.add_argument("--chat", metavar="URL", help="Open a local or configured SSH Xuloom chat in this terminal")
    parser.add_argument("-p", "--prompt", dest="prompt_text", type=str, default=None,
                        help="Send an initial prompt directly (non-interactive start)")
    parser.add_argument("-y", "--dangerously-skip-permissions", dest="auto_approve",
                        action="store_true",
                        help="Auto-approve all tool calls (use for unattended/overnight runs)")
    parser.add_argument("prompt", nargs="*", help="Optional initial prompt (positional)")
    parser.add_argument("--collaboration", choices=("solo", "auto", "ultra"), default=None,
                        help="Subagent collaboration policy; independent of reasoning effort")
    parser.add_argument("--sandbox", choices=("host", "workspace"), default=None, help="OS execution isolation; workspace fails closed if unavailable")
    args = parser.parse_args()

    linked = None
    if args.chat:
        if args.resume_id or args.continue_session or args.prompt_text or args.prompt:
            parser.error("--chat cannot be combined with a resume selector or initial prompt")
        from xuloom.features.chat_links import resolve_link, ChatLinkError
        try:
            linked = resolve_link(args.chat, require_openable=True)
        except ChatLinkError as error:
            parser.error(str(error))

    resume_id = None
    if args.continue_session:
        resume_id = "__latest__"
    elif args.resume_id:
        resume_id = args.resume_id
    if linked:
        resume_id = linked["session_id"]

    initial_prompt = args.prompt_text or (" ".join(args.prompt) if args.prompt else None)

    # Suppress Windows ProactorEventLoop cleanup noise on exit.
    # Transport.__del__ may fire after the loop is closed, which is harmless.
    if sys.platform == "win32":
        from asyncio.proactor_events import _ProactorBasePipeTransport  # type: ignore[attr-defined]
        _orig_del = _ProactorBasePipeTransport.__del__

        def _silent_del(self, *args, **kwargs):
            try:
                _orig_del(self, *args, **kwargs)
            except RuntimeError:
                pass
        _ProactorBasePipeTransport.__del__ = _silent_del

    try:
        if linked and linked.get("transport") == "ssh":
            from xuloom.remote.terminal import interactive_chat
            from xuloom.cli import EXIT_CODES
            result = asyncio.run(interactive_chat(linked["connection_id"], session_id=linked["session_id"], binding=linked["binding"],
                machine=linked["machine"], permission="full" if args.auto_approve else "ask", collaboration=args.collaboration, execution=args.sandbox))
            raise SystemExit(0 if result["ok"] else EXIT_CODES.get(result["reason"], 1))
        asyncio.run(interactive_loop(
            resume_session_id=resume_id,
            initial_prompt=initial_prompt,
            auto_approve=args.auto_approve,
            collaboration_mode=args.collaboration,
            execution_mode=args.sandbox,
            working_directory=linked["project"] if linked else None,
            strict_resume=bool(linked),
        ))
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError) as error:
        console.print(str(error), markup=False)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
