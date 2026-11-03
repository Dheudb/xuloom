"""Display-only reference parsing. Never executes or authorizes a destination."""
import re
from pathlib import Path
from urllib.parse import unquote, quote, urlsplit

CONTROL = re.compile(r'[\x00-\x1f\x7f]')


def chat_reference(value):
    if isinstance(value, str) and len(value) <= 512 and re.fullmatch(r'xuloom://chat/(?:v1/[0-9a-f]{16}|ssh/v1/[0-9a-f]{64})/[A-Za-z0-9_-]{1,128}', value):
        return value
    return None


def web_reference(value):
    if not isinstance(value, str) or CONTROL.search(value): return None
    try:
        url = urlsplit(value)
        if url.scheme.lower() in {'http', 'https'} and url.hostname and not url.username and not url.password:
            url.port  # Reject malformed ports too.
            return value
    except ValueError: pass
    return None


def file_reference(value, inline=False):
    if not isinstance(value, str) or not value or len(value) > 4096 or CONTROL.search(value) or re.search(r'[<>]', value): return None
    if re.match(r'^(?:https?|javascript|data|file|mailto|ssh|vscode|xuloom):|^[a-z]:[^\\/]', value, re.I): return None
    destination, _, fragment = value.partition('#')
    if '?' in destination: return None
    suffix = None if '#' in value else re.search(r':(\d+)(?::(\d+)|-(\d+))?$', destination)
    if suffix: destination = destination[:suffix.start()]
    if re.search(r'%(?![\da-f]{2})', destination, re.I): return None
    try: path = unquote(destination, errors='strict')
    except (UnicodeError, ValueError): return None
    if not path or CONTROL.search(path) or path.startswith(('\\\\', '//')): return None
    if re.match(r'^[a-z][\w+.-]*:', path, re.I) and not re.match(r'^[a-z]:[\\/]', path, re.I): return None
    if inline and not re.match(r'^(?:[\\/]|\.{1,2}[\\/]|[a-z]:[\\/])', path, re.I) and not re.search(r'[\\/]', path) and not re.fullmatch(r'(?:[^:]+\.[a-z\d]{1,12}|Dockerfile|Makefile|LICENSE|README|Procfile)', path, re.I): return None
    location = suffix or re.fullmatch(r'L(\d+)(?:-L(\d+))?', fragment)
    result = {'path': path}
    end = suffix[3] if suffix else location[2] if location else None
    if location and 0 < int(location[1]) <= 10000000 and (not end or int(location[1]) <= int(end) <= 10000000):
        result['line'] = int(location[1])
        if suffix and suffix[2] and 0 < int(suffix[2]) <= 10000000: result['column'] = int(suffix[2])
    return result


def terminal_file_url(target, cwd):
    path = target['path'].replace('\\', '/')
    if re.match(r'^[a-z]:/', path, re.I): url = 'file:///' + quote(path, safe='/:')
    else: url = (Path(cwd) / path).resolve().as_uri()
    if target.get('line'): url += '#L' + str(target['line'])
    return url
