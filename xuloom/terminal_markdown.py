"""Small Rich adapter: shared reference rules, terminal links and block streaming."""
from markdown_it.token import Token
from rich.markdown import Markdown
from xuloom.features.references import file_reference, web_reference, chat_reference, terminal_file_url
from xuloom.features.workspace_files import inspect_reference
import os
import re


def terminal_links(console):
    override = os.environ.get('XULOOM_HYPERLINKS')
    return override == '1' or override != '0' and console.is_terminal and console.color_system is not None and not console.legacy_windows


def file_url(target, cwd):
    if target:
        try:
            inspect_reference(cwd, target['path'])
            return terminal_file_url(target, cwd)
        except (ValueError, OSError, RuntimeError): pass
    return None


class TerminalMarkdown(Markdown):
    def __init__(self, text, cwd, *, links=True, local_files=True):
        super().__init__(text, hyperlinks=links)
        for block in self.parsed:
            if not block.children: continue
            children, output = block.children, []
            depth = 0
            for token in children:
                if token.type == 'link_open':
                    depth += 1
                    href = token.attrGet('href') or ''
                    target = file_reference(href)
                    safe = web_reference(href) or chat_reference(href) or (file_url(target, cwd) if local_files else None)
                    token.attrs = {'href': safe or ''}
                    token.meta['display_url'] = safe or (href if target and not local_files else None)
                    token.meta['plain_file'] = bool(target and not local_files)
                    output.append(token)
                elif token.type == 'link_close':
                    depth -= 1
                    opening = next((t for t in reversed(output) if t.type == 'link_open'), None)
                    if not links or opening and opening.meta.get('plain_file'):
                        href = opening.meta.get('display_url') if opening else None
                        label_text = ''.join(t.content for t in output[output.index(opening)+1:]) if opening else ''
                        if href and label_text != href:
                            label = Token('text', '', 0); label.content = ' (' + href + ')'; output.append(label)
                    output.append(token)
                elif token.type == 'code_inline' and not depth:
                    target, url = file_reference(token.content, inline=True), web_reference(token.content) or chat_reference(token.content)
                    href = url or (file_url(target, cwd) if local_files else None)
                    if href:
                        start, end = Token('link_open', 'a', 1), Token('link_close', 'a', -1)
                        start.attrSet('href', href)
                        output.extend([start, token, end])
                        if not links:
                            label = Token('text', '', 0); label.content = ' (' + href + ')'; output.append(label)
                    else: output.append(token)
                elif token.type == 'text' and not depth:
                    offset = 0
                    for match in re.finditer(r'https?://[^\s<>]+', token.content):
                        url = match[0].rstrip('.,;:!?)]}')
                        if not web_reference(url): continue
                        before = Token('text', '', 0); before.content = token.content[offset:match.start()]
                        start, label, end = Token('link_open', 'a', 1), Token('text', '', 0), Token('link_close', 'a', -1)
                        start.attrSet('href', url); label.content = url
                        output.extend([before, start, label, end]); offset = match.start()+len(url)
                    tail = Token('text', '', 0); tail.content = token.content[offset:]; output.append(tail)
                else: output.append(token)
            block.children = output


class MarkdownStream:
    """Render complete blocks with Rich; keep incomplete links/fences buffered."""
    def __init__(self, console, cwd, links=True, local_files=True):
        self.console, self.cwd, self.links = console, cwd, links
        self.local_files = local_files
        self.buffer = ''

    def append(self, text):
        self.buffer += text
        if re.search(r'\[[^\]\n]+\](?!\()', self.buffer): return
        # Use the whole pending block so split delimiters cannot become links early.
        while self.buffer:
            fence, end, consumed = None, 0, False
            for line in self.buffer.splitlines(keepends=True):
                if not line.endswith(('\n', '\r')): break
                match = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
                if match:
                    marker = match[1]
                    if fence is None: fence = marker
                    elif marker[0] == fence[0] and len(marker) >= len(fence) and not line[match.end():].strip(): fence = None
                end += len(line)
                if fence is None and not line.strip():
                    self.render(self.buffer[:end]); self.buffer = self.buffer[end:]; consumed = True; break
            if not consumed: break

    def render(self, text):
        if text.strip(): self.console.print(TerminalMarkdown(text, self.cwd, links=self.links, local_files=self.local_files))

    def finish(self):
        self.render(self.buffer); self.buffer = ''
