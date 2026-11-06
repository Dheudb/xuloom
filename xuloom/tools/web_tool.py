import re
import asyncio
import sys
import httpx
from bs4 import BeautifulSoup
if sys.version_info >= (3, 10):
    from ddgs import DDGS
    from ddgs.exceptions import TimeoutException as SearchTimeout, RatelimitException as SearchRateLimit
else:
    from duckduckgo_search import DDGS
    from duckduckgo_search.exceptions import TimeoutException as SearchTimeout, RatelimitException as SearchRateLimit
from pydantic import BaseModel, Field, model_validator
from xuloom.tools.base import BaseAgenticTool
from xuloom.context.tool_results import build_tool_result

MAX_SEARCH_RESULTS = 50
MAX_FETCH_BYTES = 5_000_000


def network_error(error, metadata):
    status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
    code = ("rate_limited" if status == 429 or isinstance(error, SearchRateLimit) else "access_denied" if status in {401, 403} else "http_error" if status is not None else
            "timeout" if isinstance(error, (TimeoutError, httpx.TimeoutException, SearchTimeout)) else
            "connection_error" if isinstance(error, httpx.NetworkError) else "provider_error")
    retryable = status in {408, 425, 429} or status is not None and status >= 500 or code in {"timeout", "connection_error", "rate_limited"}
    return {**metadata, "reason": type(error).__name__, "code": code,
            "http_status": status, "retryable": bool(retryable)}


def extract_page(text, html):
    if not html:
        return text, ""
    soup = BeautifulSoup(text, 'lxml')
    title = soup.title.get_text(" ", strip=True)[:300] if soup.title else ""
    for element in soup(["script", "style", "nav", "footer", "meta", "noscript"]):
        element.decompose()
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
        try:
            limit = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
            options = self.context.get('native_search_options')
            if callable(options):
                backend = options()['profile']['backend']
                if backend != 'builtin':
                    from xuloom.tools.native_search import execute
                    return await execute(self.context, query, limit)
                backend = 'ddgs'
            # DDGS is synchronous; its network calls must not block streaming or Stop.
            results = await asyncio.wait_for(asyncio.to_thread(lambda: list(DDGS(timeout=8).text(query, max_results=limit))), timeout=35)
            results = [r for r in results if isinstance(r, dict) and str(r.get('href', '')).startswith(('https://', 'http://'))][:limit]
            if not results:
                return build_tool_result(ok=False, is_error=True, content="The built-in web search service returned no usable results. Try a revised query or a configured search MCP. The web search tool exists; local project files are not a substitute for this public-web research.", metadata={"backend": "ddgs", "query": query, "reason": "no_results"})
            out = f"Web Search Results for: '{query}'\n" + "="*40 + "\n"
            for r in results:
                out += f"Title: {str(r.get('title', ''))[:500]}\nURL: {r.get('href')}\nSnippet: {str(r.get('body', ''))[:2500]}\n\n"
            return build_tool_result(ok=True, content=out, metadata={"backend": "ddgs", "query": query, "result_count": len(results),
                "sources_in_content": True, "sources": [{"url": r["href"], "title": str(r.get("title", "")), "kind": "search"} for r in results]})
        except Exception as e:
            return build_tool_result(ok=False, is_error=True, content=f"Search service {backend} failed ({type(e).__name__}). This is not a missing tool. Check API/model support and network access; no fallback provider was called.", metadata=network_error(e, {"backend": backend, "query": query}))

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
        try:
            target = httpx.URL(url)
            if target.scheme not in {"http", "https"} or not target.host or target.username or target.password:
                return build_tool_result(ok=False, is_error=True, content="Use an HTTP(S) URL without embedded credentials.", metadata={"code": "invalid_url", "retryable": False})
            options = self.context.get('native_search_options')
            if callable(options) and options()['profile']['backend'] == 'deepseek':
                from xuloom.tools.native_search import execute
                return await execute(self.context, prompt, 1, url=url)
            async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    media_type = resp.headers.get("content-type", "text/html").split(";", 1)[0].lower()
                    if not (media_type.startswith("text/") or media_type in {"application/xhtml+xml", "application/json", "application/xml"}):
                        return build_tool_result(ok=False, is_error=True, content="This URL returns a binary document, not readable webpage text.", metadata={"code": "unsupported_media_type", "media_type": media_type, "retryable": False})
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        if len(body) + len(chunk) > MAX_FETCH_BYTES:
                            return build_tool_result(ok=False, is_error=True, content="Webpage exceeds the 5 MB response limit; no partial page was returned as complete.", metadata={"code": "response_too_large", "retryable": False})
                        body.extend(chunk)
                    source = bytes(body).decode(resp.encoding or "utf-8", errors="replace")
            text, title = await asyncio.to_thread(extract_page, source, media_type in {"text/html", "application/xhtml+xml"})

            result = f"Content from {resp.url}:\n\n{text}"
            if prompt:
                result += f"\n\n---\nExtraction prompt: {prompt}"
            return build_tool_result(ok=True, content=result, metadata={"url": str(resp.url), "source_bytes": len(body),
                "content_chars": len(text), "media_type": media_type, "truncated": False,
                "sources_in_content": True, "sources": [{"url": str(resp.url), "title": title or str(resp.url), "kind": "page"}]})

        except Exception as e:
            metadata = network_error(e, {"url": url})
            content = (f"Website refused access (HTTP {metadata['http_status']}); page content was not read. Use an accessible publisher abstract, repository or another source; repeating the same URL will not resolve an access denial."
                if metadata["code"] == "access_denied" else f"Webpage request failed ({type(e).__name__}). Check the URL and network, or try another source.")
            return build_tool_result(ok=False, is_error=True, content=content, metadata=metadata)
