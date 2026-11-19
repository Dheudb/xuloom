import re
import json
import asyncio
import httpx
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from ddgs import DDGS
from pydantic import BaseModel, Field, model_validator
from xuloom.tools.base import BaseAgenticTool
from xuloom.context.tool_results import build_tool_result
from xuloom.tools.web_network import (network_error, SearchTimeout, SearchRateLimit,  # noqa: F401 - re-exported for callers
    search_network, run_ddgs, diagnostic_secrets, web_network_mode, fetch_page, safe_detail)

MAX_SEARCH_RESULTS = 50
MAX_FETCH_BYTES = 5_000_000
SEARCH_TOTAL_TIMEOUT = 35
SEARCH_MAX_ATTEMPTS = 2
SEARCH_RETRY_DELAY = 0.75
FETCH_TOTAL_TIMEOUT = 35


async def builtin_search(factory, query, limit, options, abort=None):
    mode, proxies, route = search_network(web_network_mode(options))
    secrets = diagnostic_secrets(options, proxies)
    deadline = asyncio.get_running_loop().time() + SEARCH_TOTAL_TIMEOUT
    failures = []
    for attempt in range(1, SEARCH_MAX_ATTEMPTS + 1):
        if abort is not None and abort.is_set(): raise asyncio.CancelledError()
        remaining = max(0, deadline - asyncio.get_running_loop().time())
        try:
            # One owned synchronous attempt at a time. A deadline/cancellation
            # stops local waiting; never start a second overlapping DDGS thread.
            rows = await asyncio.wait_for(asyncio.to_thread(run_ddgs, factory, query, limit,
                proxies, route, min(8, remaining)), timeout=remaining)
            return rows, {**route, "attempts": attempt, **({"retry_history": failures} if failures else {})}
        except asyncio.TimeoutError as error:
            diagnostic = network_error(error, {**route, "attempts": attempt}, secrets)
            diagnostic.update(code="timeout", retryable=True)
            diagnostic["retry_skipped"] = "overall_deadline"
            diagnostic["detail"] = f"DDGS exceeded its {SEARCH_TOTAL_TIMEOUT:g}s overall deadline; no overlapping retry was started."
            return None, {**diagnostic, "retry_history": failures}
        except Exception as error:
            diagnostic = network_error(error, {}, secrets)
            failures.append({"attempt": attempt, **diagnostic})
            result = {**route, **diagnostic, "attempts": attempt, "retry_history": failures}
            delay = max(SEARCH_RETRY_DELAY, diagnostic.get("retry_after_seconds", 0))
            if not diagnostic["retryable"]: return None, result
            if attempt >= SEARCH_MAX_ATTEMPTS: return None, {**result, "automatic_retry_exhausted": True}
            if delay > 3 or deadline - asyncio.get_running_loop().time() <= delay:
                return None, {**result, "retry_skipped": "cooldown_or_deadline"}
            if abort is None:
                await asyncio.sleep(delay)
            else:
                try:
                    await asyncio.wait_for(abort.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                if abort.is_set(): raise asyncio.CancelledError()


def extract_page(text, html, url=""):
    if not html:
        # Apple's DocC API wraps a short article in a large JSON navigation
        # graph. Expose the article and referenced links, not thousands of
        # unrelated reference objects that force the agent to page raw JSON.
        if httpx.URL(url).host == "developer.apple.com":
            try:
                document = json.loads(text)
            except ValueError:
                document = None
            if isinstance(document, dict) and isinstance(document.get("schemaVersion"), dict) and isinstance(document.get("primaryContentSections"), list):
                references = document.get("references", {})
                def readable(node):
                    if isinstance(node, list): return "".join(readable(child) for child in node)
                    if not isinstance(node, dict): return ""
                    kind = node.get("type")
                    if kind == "text": return str(node.get("text", ""))
                    if kind == "codeVoice": return str(node.get("code", ""))
                    if kind == "reference":
                        reference = references.get(node.get("identifier"), {}) if isinstance(references, dict) else {}
                        title = str(reference.get("title", node.get("identifier", "")))
                        target = str(reference.get("url", ""))
                        if target.startswith("/"): target = "https://developer.apple.com" + target
                        return f"{title} ({target})" if target.startswith(("https://", "http://")) else title
                    if kind == "codeListing": return "\n" + "\n".join(map(str, node.get("code", []))) + "\n"
                    if kind == "heading": return str(node.get("text", "")) + "\n"
                    body = "".join(readable(node[key]) for key in ("inlineContent", "content", "items", "rows", "cells") if key in node)
                    return body + ("\n\n" if kind in {"paragraph", "orderedList", "unorderedList", "aside"} else "")
                article = readable(document["primaryContentSections"]).strip()
                if article:
                    return article, str(document.get("metadata", {}).get("title", ""))[:300]
        return text, ""
    soup = BeautifulSoup(text, 'lxml')
    title = soup.title.get_text(" ", strip=True)[:300] if soup.title else ""
    for element in soup(["script", "style", "nav", "footer", "meta", "noscript"]):
        element.decompose()
    # Keep actual destinations rather than breadcrumb text that invites guessed URLs.
    base = soup.find("base", href=True)
    base_url = urljoin(url, str(base["href"])) if base else url
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith("#"): continue
        destination = urljoin(base_url, href)
        try:
            parsed = urlsplit(destination)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password: continue
        except ValueError: continue
        if destination in seen or len(seen) >= 200 or len(destination) > 2048: continue
        seen.add(destination)
        label = anchor.get_text(" ", strip=True)
        anchor.replace_with(f"{label} ({destination})" if label and label != destination else destination)
    return re.sub(r'\s+', ' ', soup.get_text(separator=' ', strip=True)), title

class WebSearchInput(BaseModel):
    query: str = Field(..., description="The search query to look up on the web.")
    max_results: int = Field(5, ge=1, le=MAX_SEARCH_RESULTS,
        description="Maximum number of results to request (1–50, default 5). Use 15–30 for broader research when useful; the provider may return fewer results.")

class WebSearchTool(BaseAgenticTool):
    name = "web_search_tool"
    description = "Search the web for real-time information about any topic. Returns summarized information from search results and relevant URLs."
    input_schema = WebSearchInput
    is_read_only = True
    risk_level = "low"

    def prompt(self) -> str:
        import datetime
        month_year = datetime.datetime.now().strftime("%B %Y")
        return f"""Search the web for real-time information about any topic. Returns search result information formatted as search result blocks, including links as markdown hyperlinks.

Use this tool for public web research or current information. Discover it with tool_search_tool if not loaded. It uses the connection's configured search backend. Inspect local files only for project-specific requests.
If the search service fails, report the service error and try a revised query or a configured search MCP. A provider failure does not mean the tool is unavailable.
max_results accepts 1–50 (default 5). Request more results for broad research when needed, not automatically for every query. The search provider may return fewer; this is a maximum, not a guaranteed count.

CRITICAL REQUIREMENT:
- After answering the user's question, you MUST include a "Sources:" section at the end of your response
- In the Sources section, list all relevant URLs from the search results as markdown hyperlinks: [Title](URL)

IMPORTANT - Use the correct year in search queries:
- The current month is {month_year}. Include an appropriate year when recency matters without excluding relevant earlier research."""

    async def execute(self, query: str, max_results: int = 5) -> dict:
        backend = 'ddgs'
        settings = {}
        try:
            limit = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
            options = self.context.get('native_search_options')
            if callable(options):
                settings = options()
                backend = settings['profile']['backend']
                if backend != 'builtin':
                    from xuloom.tools.native_search import execute
                    return await execute(self.context, query, limit)
                backend = 'ddgs'
            # DDGS is synchronous; its network calls must not block streaming or Stop.
            results, diagnostic = await builtin_search(DDGS, query, limit, settings, self.context.get("abort_event"))
            if results is None:
                return build_tool_result(ok=False, is_error=True,
                    content=f"Search service ddgs failed ({diagnostic['reason']}; {diagnostic['code']}; {diagnostic['attempts']} attempt(s)). This is not a missing tool. Inspect the retained diagnostics and connection network settings; no fallback provider was called.",
                    metadata={"backend": "ddgs", "query": query, **diagnostic})
            results = [r for r in results if isinstance(r, dict) and str(r.get('href', '')).startswith(('https://', 'http://'))][:limit]
            if not results:
                return build_tool_result(ok=False, is_error=True, content="The built-in web search service returned no usable results. Try a revised query or a configured search MCP. The web search tool exists; local project files are not a substitute for this public-web research.", metadata={"backend": "ddgs", "query": query, **diagnostic, "reason": "no_results", "code": "no_results", "retryable": False})
            out = f"Web Search Results for: '{query}'\n" + "="*40 + "\n"
            for r in results:
                out += f"Title: {str(r.get('title', ''))[:500]}\nURL: {r.get('href')}\nSnippet: {str(r.get('body', ''))[:2500]}\n\n"
            return build_tool_result(ok=True, content=out, metadata={"backend": "ddgs", "query": query, **diagnostic, "result_count": len(results),
                "sources_in_content": True, "sources": [{"url": r["href"], "title": str(r.get("title", "")), "kind": "search"} for r in results]})
        except Exception as e:
            return build_tool_result(ok=False, is_error=True, content=f"Search service {backend} failed ({type(e).__name__}). This is not a missing tool. Check API/model support and network access; no fallback provider was called.", metadata=network_error(e, {"backend": backend, "query": query}, [settings.get("api_key", "")]))

    def input_error_hint(self) -> str:
        return 'Use {"query":"your search terms","max_results":15}. max_results must be between 1 and 50; omit it for the default of 5. The provider may return fewer results.'

class WebFetchInput(BaseModel):
    url: str = Field(..., description="The URL to fetch content from.")
    prompt: str = Field("", description="Optional description of what information to extract from the page.")

    @model_validator(mode="before")
    @classmethod
    def canonical_url(cls, values):
        # A tool-local alias; authorization still inspects the canonical URL.
        if isinstance(values, dict) and "file_path" in values:
            alias = values["file_path"]
            if isinstance(alias, str) and alias.startswith(("https://", "http://")):
                if "url" in values and values["url"] != alias:
                    raise ValueError("Conflicting url and file_path; provide one URL")
                values = {key: value for key, value in values.items() if key != "file_path"}
                values["url"] = alias
        return values

class WebFetchTool(BaseAgenticTool):
    name = "web_fetch_tool"
    description = "Fetch content from a specified URL and return its contents in a readable format. Use a prompt to specify what information to extract."
    input_schema = WebFetchInput
    is_read_only = True
    risk_level = "low"

    def prompt(self) -> str:
        return """Fetches static HTML from a URL, removes scripts and navigation, and returns plain text for you to analyze. The optional extraction prompt is included with the returned text; no separate AI extraction is performed.

Usage notes:
- The URL must be a fully-formed valid URL
- The prompt should describe what information you want to extract from the page
- This tool is read-only and does not modify any files
- Full extracted text uses the common output archive when large. Responses over 5 MB or binary documents are rejected explicitly. JavaScript is not executed; use browser_tool for dynamic pages.
- Redirects are followed and the final URL is reported.
- For GitHub URLs, prefer using the gh CLI via bash_tool instead (e.g., gh pr view, gh issue view, gh api)"""

    async def execute(self, url: str, prompt: str = "", **kwargs) -> dict:
        info, secrets = {"url": safe_detail(url)}, []
        try:
            target = httpx.URL(url)
            if target.scheme not in {"http", "https"} or not target.host or target.username or target.password:
                return build_tool_result(ok=False, is_error=True, content="Use an HTTP(S) URL without embedded credentials.", metadata={"code": "invalid_url", "retryable": False})
            options = self.context.get('native_search_options')
            settings = options() if callable(options) else {}
            if settings.get('profile', {}).get('backend') == 'deepseek':
                from xuloom.tools.native_search import execute
                return await execute(self.context, prompt, 1, url=url)
            body, media_type, final_url, encoding = await asyncio.wait_for(fetch_page(url, settings, info, MAX_FETCH_BYTES, secrets), timeout=FETCH_TOTAL_TIMEOUT)
            if body is None:
                oversized = media_type == "too_large"
                return build_tool_result(ok=False, is_error=True,
                    content="Webpage exceeds the 5 MB response limit; no partial page was returned as complete." if oversized else "This URL returns a binary document, not readable webpage text.",
                    metadata={**info, "code": "response_too_large" if oversized else "unsupported_media_type", "media_type": media_type, "retryable": False})
            source = body.decode(encoding or "utf-8", errors="replace")
            text, title = await asyncio.to_thread(extract_page, source, media_type in {"text/html", "application/xhtml+xml"}, final_url)
            if not text.strip():
                return build_tool_result(ok=False, is_error=True, content="Static fetch returned no readable text. The page may require JavaScript or login; browser_tool can inspect it with normal permissions.",
                    metadata={**info, "code": "no_readable_content", "retryable": False})
            result = f"Content from {final_url}:\n\n{text}"
            if prompt: result += f"\n\n---\nExtraction prompt: {prompt}"
            return build_tool_result(ok=True, content=result, metadata={**info, "url": final_url, "source_bytes": len(body),
                "content_chars": len(text), "media_type": media_type, "truncated": False,
                "sources_in_content": True, "sources": [{"url": final_url, "title": title or final_url, "kind": "page"}]})
        except Exception as error:
            metadata = network_error(error, info, secrets)
            code = metadata["code"]
            if code == "legal_restriction":
                content = "Website returned HTTP 451 at the final URL; page content was not read. This status does not establish the resource's existence or any person's identity. Use another accessible source."
            elif code == "access_denied":
                content = f"Website refused access (HTTP {metadata['http_status']}); page content was not read. Login or site access restrictions may apply; repeating the same URL will not resolve an access denial."
            elif code == "not_found": content = "Website returned HTTP 404 at the final URL. No page content was read; verify the actual source link instead of guessing a path."
            elif code == "rate_limited": content = "Website rate limit reached (HTTP 429). Respect Retry-After and retry later; no alternate network route was used to repeat the refusal."
            else: content = f"Webpage request failed ({type(error).__name__}); inspect the recorded routes and final URL, or try another source."
            return build_tool_result(ok=False, is_error=True, content=content, metadata=metadata)
