import os
import time
import uuid
import base64
import asyncio
from pathlib import Path
from importlib.util import find_spec

PLAYWRIGHT_AVAILABLE = find_spec('playwright') is not None


def async_playwright():
    from playwright.async_api import async_playwright as factory
    return factory()


class BrowserSessionManager:
    """
    Lightweight single-page browser session manager backed by Playwright.
    """

    def __init__(self, artifact_dir=".xuloom/browser"):
        self.artifact_dir = artifact_dir
        self._playwright = None
        self._browser = None
        self._page = None
        self._context = None
        self._last_url = None
        self.capture_callback = None
        self._startup_lock = asyncio.Lock()
        self._render_contexts = set()

    async def render(self, *, url=None, file_path=None, width=1440, height=960, wait_ms=500, full_page=True, artifact_dir=None, cwd=None):
        """One-shot render; desktop reuses its Chromium, CLI uses optional Playwright."""
        if self.capture_callback:
            result = await self.capture_callback(dict(url=url, file_path=file_path, width=width,
                height=height, wait_ms=wait_ms, full_page=full_page))
            if not isinstance(result, dict):
                raise RuntimeError("Screenshot host returned no valid result")
            if result.get("error"):
                raise RuntimeError(result["error"])
            data = base64.b64decode(result.pop("png_base64"), validate=True)
        else:
            from xuloom.features.browser_render import capture
            data, result = await asyncio.wait_for(capture(self, cwd=cwd or os.getcwd(), url=url, file_path=file_path,
                width=width, height=height, wait_ms=wait_ms, full_page=full_page), 40)
        from xuloom.context.tool_media import MAX_BYTES, preview_image_bytes
        if len(data) > MAX_BYTES:
            raise ValueError("Screenshot exceeds the image size limit")
        image = preview_image_bytes(data)
        directory = Path(artifact_dir or self.artifact_dir)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / ("screenshot-" + uuid.uuid4().hex + ".png")
        from xuloom.core.storage import atomic_bytes
        atomic_bytes(target, data)
        self._last_url = result.get("url")
        return {**result, "path": str(target.resolve()), "width": image["width"], "height": image["height"]}

    async def ensure_browser(self):
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "Playwright is not installed on this execution host. Install `pip install \"xuloom[browser]\"` "
                "and then run `python -m playwright install chromium` on that host."
            )

        async with self._startup_lock:
            if self._browser is None:
                try:
                    self._playwright = await async_playwright().start()
                    self._browser = await self._playwright.chromium.launch(headless=True)
                except BaseException:
                    try: await self._close_handles()
                    except Exception: pass  # Preserve the startup/cancellation error.
                    raise
            return self._browser

    async def ensure_page(self):
        await self.ensure_browser()
        async with self._startup_lock:
            if self._page is None:
                try:
                    self._context = await self._browser.new_context(viewport={"width": 1440, "height": 960})
                    self._page = await self._context.new_page()
                except BaseException:
                    try: await self._close_handles()
                    except Exception: pass
                    raise
            return self._page

    async def navigate(self, url: str, wait_until: str = "domcontentloaded"):
        page = await self.ensure_page()
        response = await page.goto(url, wait_until=wait_until, timeout=30000)
        self._last_url = page.url
        return {
            "url": page.url,
            "status": response.status if response else "unknown",
            "title": await page.title(),
        }

    async def snapshot(self):
        page = await self.ensure_page()
        title = await page.title()
        url = page.url
        text_preview = await page.evaluate(
            """
            () => {
              const text = (document.body?.innerText || '').trim().replace(/\\s+/g, ' ');
              return text.slice(0, 2500);
            }
            """
        )
        interactive = await page.evaluate(
            """
            () => {
              const nodes = Array.from(document.querySelectorAll('a, button, input, textarea, select, [role="button"]')).slice(0, 25);
              return nodes.map((node, index) => {
                const text = (node.innerText || node.value || node.getAttribute('aria-label') || node.getAttribute('placeholder') || '').trim().replace(/\\s+/g, ' ');
                return {
                  index: index + 1,
                  tag: node.tagName.toLowerCase(),
                  text: text.slice(0, 120),
                  selector: node.id ? `#${node.id}` : null
                };
              });
            }
            """
        )
        self._last_url = url
        return {
            "url": url,
            "title": title,
            "text_preview": text_preview,
            "interactive_elements": interactive,
        }

    async def click(self, selector: str):
        page = await self.ensure_page()
        await page.locator(selector).first.click(timeout=15000)
        self._last_url = page.url
        return {"url": page.url, "title": await page.title(), "selector": selector}

    async def type(self, selector: str, text: str, clear: bool = True):
        page = await self.ensure_page()
        locator = page.locator(selector).first
        if clear:
            await locator.fill(text, timeout=15000)
        else:
            await locator.type(text, timeout=15000)
        self._last_url = page.url
        return {"url": page.url, "title": await page.title(), "selector": selector, "characters": len(text)}

    async def wait(self, seconds: float = 1.0):
        page = await self.ensure_page()
        await page.wait_for_timeout(max(0, seconds) * 1000)
        self._last_url = page.url
        return {"url": page.url, "title": await page.title(), "waited_seconds": seconds}

    async def screenshot(self, path: str = None, full_page: bool = True):
        page = await self.ensure_page()
        os.makedirs(self.artifact_dir, exist_ok=True)
        if not path:
            path = os.path.join(self.artifact_dir, f"screenshot-{int(time.time())}.png")
        await page.screenshot(path=path, full_page=full_page)
        self._last_url = page.url
        return {"url": page.url, "title": await page.title(), "path": os.path.abspath(path)}

    async def close(self):
        async with self._startup_lock:
            await self._close_handles()

    async def _close_handles(self):
        owned = [(item, 'close') for item in self._render_contexts] + [(self._page, "close"), (self._context, "close"), (self._browser, "close"), (self._playwright, "stop")]
        self._render_contexts.clear()
        self._page = self._context = self._browser = self._playwright = None
        self._last_url = None
        errors = []
        for handle, method in owned:
            if handle is not None:
                try:
                    await getattr(handle, method)()
                except BaseException as error:
                    errors.append(error)
        if errors:
            raise errors[0]

    def export_state(self):
        return {
            "last_url": self._last_url,
            "available": bool(self.capture_callback or PLAYWRIGHT_AVAILABLE),
        }

    def load_state(self, payload: dict):
        if not isinstance(payload, dict):
            self._last_url = None
            return

        self._last_url = payload.get("last_url") or None
