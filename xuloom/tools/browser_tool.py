import os
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal
from pydantic import BaseModel, Field
from xuloom.tools.base import BaseAgenticTool
from xuloom.context.tool_results import build_tool_result


class BrowserToolInput(BaseModel):
    action: Literal["render", "navigate", "snapshot", "click", "type", "wait", "screenshot", "close"] = Field(
        ...,
        description="One of: render, navigate, snapshot, click, type, wait, screenshot, close. render captures a URL or local HTML in a hidden browser and returns an image for visual verification.",
    )
    url: str = Field(None, description="Target URL for navigate.")
    selector: str = Field(None, description="CSS selector for click or type.")
    text: str = Field(None, description="Input text for type.")
    seconds: float = Field(1.0, description="Seconds to wait for the wait action.")
    path: str = Field(None, description="Optional screenshot output path.")
    full_page: bool = Field(True, description="Whether screenshots should capture the full page.")
    clear: bool = Field(True, description="Whether type should replace existing input value.")
    file_path: str = Field(None, description="For render: local HTML file inside the current workspace; provide this OR url.")
    width: int = Field(1440, ge=320, le=2560, description="Render viewport width in CSS pixels.")
    height: int = Field(960, ge=240, le=2160, description="Render viewport height in CSS pixels.")
    wait_ms: int = Field(500, ge=0, le=10000, description="For render: wait after page load for animation or asynchronous content.")


class BrowserTool(BaseAgenticTool):
    workspace_paths = {"path": False, "file_path": False}
    name = "browser_tool"
    description = "Render workspace HTML or a website in a hidden browser and return a screenshot to inspect UI changes. Also supports browser navigation, DOM inspection, clicking, typing and screenshots. Local desktop render reuses Chromium; CLI and SSH render require the browser extra and Chromium on the execution host."
    input_schema = BrowserToolInput
    risk_level = "high"

    def prompt(self) -> str:
        return (
            "Use `browser_tool` when static fetch/search is insufficient and the task requires real page interaction, DOM inspection, waiting, or screenshots. "
            "Prefer `snapshot` before click/type so you have fresh page structure."
        )

    def validate_input(
        self,
        action: str,
        url: str = None,
        selector: str = None,
        text: str = None,
        seconds: float = 1.0,
        path: str = None,
        full_page: bool = True,
        clear: bool = True,
        file_path=None, width=1440, height=960, wait_ms=500,
    ):
        valid_actions = {"render", "navigate", "snapshot", "click", "type", "wait", "screenshot", "close"}
        if action not in valid_actions:
            return f"Unsupported browser_tool action '{action}'."
        if action == "navigate" and not url:
            return "url is required when action='navigate'."
        if action in {"click", "type"} and not selector:
            return f"selector is required when action='{action}'."
        if action == "type" and text is None:
            return "text is required when action='type'."
        if action == "render":
            if bool(url) == bool(file_path):
                return "render requires exactly one of url or file_path."
            if url:
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                    return "render url must be HTTP(S) without embedded credentials; use file_path for local HTML."
            if file_path:
                root = Path(self.context.get("session_cwd", os.getcwd())).resolve()
                target = (root / file_path).resolve()
                if (root != target and root not in target.parents) or target.suffix.lower() not in {".html", ".htm"}:
                    return "render file_path must be an HTML file inside the current workspace."
        return None

    def build_permission_summary(
        self,
        action: str,
        url: str = None,
        selector: str = None,
        text: str = None,
        seconds: float = 1.0,
        path: str = None,
        full_page: bool = True,
        clear: bool = True,
        file_path=None, width=1440, height=960, wait_ms=500,
    ) -> str:
        text_preview = (text or "")[:120] + ("..." if text and len(text) > 120 else "")
        return (
            "Browser automation requested.\n"
            f"action: {action}\n"
            f"url: {url or '<none>'}\n"
            f"selector: {selector or '<none>'}\n"
            f"text_preview: {text_preview or '<empty>'}\n"
            f"seconds: {seconds}\n"
            f"path: {path or '<auto>'}\nfile_path: {file_path or '<none>'}\nviewport: {width}x{height}"
        )

    async def execute(
        self,
        action: str,
        url: str = None,
        selector: str = None,
        text: str = None,
        seconds: float = 1.0,
        path: str = None,
        full_page: bool = True,
        clear: bool = True,
        file_path=None, width=1440, height=960, wait_ms=500,
    ) -> dict:
        browser = self.context.get("browser_manager")
        if not browser:
            return build_tool_result(
                ok=False,
                content="Browser manager is unavailable.",
                metadata={"action": action},
                is_error=True,
            )

        try:
            if action == "render":
                from xuloom.core.storage import artifact_directory
                sid = self.context.get("session_id")
                directory = str(artifact_directory(self.context.get("session_cwd")) / sid / "browser") if sid else None
                if file_path:
                    file_path = str((Path(self.context.get("session_cwd", os.getcwd())) / file_path).resolve(strict=True))
                result = await browser.render(url=url, file_path=file_path, width=width, height=height,
                    wait_ms=wait_ms, full_page=full_page, artifact_dir=directory, cwd=self.context.get("session_cwd"))
                return await self._image_result(result, action)
            if action == "navigate":
                if not url:
                    return build_tool_result(
                        ok=False,
                        content="url is required for navigate.",
                        metadata={"action": action},
                        is_error=True,
                    )
                result = await browser.navigate(url)
                return build_tool_result(
                    ok=True,
                    content=f"Navigated to {result['url']} ({result['title']})",
                    metadata={"action": action, **result},
                )

            if action == "snapshot":
                result = await browser.snapshot()
                interactive_lines = []
                for item in result.get("interactive_elements", []):
                    label = item.get("text") or "<no label>"
                    selector_hint = item.get("selector") or "<no id selector>"
                    interactive_lines.append(f"{item.get('index')}. <{item.get('tag')}> {label} [{selector_hint}]")
                content = (
                    f"Title: {result.get('title')}\n"
                    f"URL: {result.get('url')}\n"
                    f"Text preview:\n{result.get('text_preview', '')}\n\n"
                    f"Interactive elements:\n" + ("\n".join(interactive_lines) if interactive_lines else "None detected.")
                )
                return build_tool_result(
                    ok=True,
                    content=content,
                    metadata={
                        "action": action,
                        "url": result.get("url"),
                        "title": result.get("title"),
                        "interactive_count": len(result.get("interactive_elements", [])),
                    },
                )

            if action == "click":
                if not selector:
                    return build_tool_result(
                        ok=False,
                        content="selector is required for click.",
                        metadata={"action": action},
                        is_error=True,
                    )
                result = await browser.click(selector)
                return build_tool_result(
                    ok=True,
                    content=f"Clicked '{selector}' on {result['url']}.",
                    metadata={"action": action, **result},
                )

            if action == "type":
                if not selector:
                    return build_tool_result(
                        ok=False,
                        content="selector is required for type.",
                        metadata={"action": action},
                        is_error=True,
                    )
                if text is None:
                    return build_tool_result(
                        ok=False,
                        content="text is required for type.",
                        metadata={"action": action},
                        is_error=True,
                    )
                result = await browser.type(selector, text, clear=clear)
                return build_tool_result(
                    ok=True,
                    content=f"Typed into '{selector}' on {result['url']}.",
                    metadata={"action": action, "clear": clear, **result},
                )

            if action == "wait":
                result = await browser.wait(seconds)
                return build_tool_result(
                    ok=True,
                    content=f"Waited {seconds} second(s) on {result['url']}.",
                    metadata={"action": action, **result},
                )

            if action == "screenshot":
                normalized_path = os.path.abspath(path) if path else None
                result = await browser.screenshot(normalized_path, full_page=full_page)
                return await self._image_result(result, action)

            if action == "close":
                await browser.close()
                return build_tool_result(
                    ok=True,
                    content="Browser session closed.",
                    metadata={"action": action},
                )

            return build_tool_result(
                ok=False,
                content=f"Unsupported browser action '{action}'.",
                metadata={"action": action},
                is_error=True,
            )
        except Exception as e:
            return build_tool_result(
                ok=False,
                content=f"Browser action failed: {str(e)}",
                metadata={"action": action, "url": url or "", "selector": selector or ""},
                is_error=True,
            )

    async def _image_result(self, result, action):
        from xuloom.tools.file_read_tool import FileReadTool
        reader = FileReadTool()
        reader.context = self.context
        content = await reader.execute(file_path=result["path"])
        if not isinstance(content, list):
            # Keep the saved screenshot, but never claim a text-only model saw it.
            return build_tool_result(ok=True, content=f"Screenshot saved to {result['path']}. Visual inspection unavailable: {content}",
                metadata={"action": action, "image_attached": False, **result})
        content.insert(0, {"type": "text", "text": "Rendered screenshot; inspect this image to verify the UI. " +
            ("Page exceeded the capture limit; viewport only. " if result.get("truncated") else "")})
        return build_tool_result(ok=True, content=content, metadata={"action": action, "image_attached": True, **result})
