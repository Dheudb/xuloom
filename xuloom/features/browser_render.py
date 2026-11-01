"""One-shot headless rendering using the existing authorized HTML asset reader."""
import asyncio
import base64
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
import uuid

from xuloom.features import html_preview

CSP = "default-src 'self' data: blob: https: http:; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: https: http:; style-src 'self' 'unsafe-inline' https: http:; object-src 'none'; frame-src 'none'; base-uri 'self'; form-action 'none'"


def resource(cwd, entry, asset):
    """Use the same limits, ownership and version checks as desktop/SSH preview."""
    info = html_preview.info(cwd, entry, [], asset)
    content = bytearray()
    while len(content) < info['size']:
        row = html_preview.read(cwd, entry, [], info['asset'], info['version'], len(content))
        data = base64.b64decode(row['data'], validate=True)
        if not data or row['offset'] != len(content) or row['nextOffset'] != len(content) + len(data):
            raise ValueError('HTML asset returned an invalid range')
        content.extend(data)
    if len(content) != info['size']: raise ValueError('HTML asset changed during rendering')
    return info, bytes(content)


async def capture(manager, *, cwd, file_path, url, width, height, wait_ms, full_page):
    if bool(file_path) == bool(url): raise ValueError('Provide one workspace HTML file or HTTP(S) URL')
    if any(type(value) is not int for value in (width, height, wait_ms)) or not 320 <= width <= 2560 or not 240 <= height <= 2160 or not 0 <= wait_ms <= 10000:
        raise ValueError('Invalid screenshot viewport or wait_ms')
    if type(full_page) is not bool: raise ValueError('full_page must be boolean')
    origin = 'https://' + uuid.uuid4().hex + '.xuloom.invalid'
    if file_path:
        cwd = str(Path(cwd).resolve())
        file = await asyncio.to_thread(html_preview.info, cwd, str(file_path), [])
        target = origin + '/' + quote(file['asset'], safe='/')
    else:
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Only HTTP(S) URLs without embedded credentials are allowed')
        target = url
    browser = await manager.ensure_browser()
    context = await browser.new_context(viewport={'width': width, 'height': height},
        device_scale_factor=1, accept_downloads=False, service_workers='block')
    manager._render_contexts.add(context)
    try:
        async def route(request):
            parsed = urlsplit(request.request.url)
            if parsed.scheme not in {'http', 'https', 'data', 'blob', 'about'}:
                await request.abort(); return
            if file_path and parsed.hostname == urlsplit(origin).hostname:
                if parsed.scheme != 'https' or parsed.port is not None or parsed.username or parsed.password or request.request.method not in {'GET', 'HEAD'}:
                    await request.abort(); return
                try:
                    info, content = await asyncio.to_thread(resource, cwd, file['path'], unquote(parsed.path).lstrip('/'))
                except (OSError, ValueError, RuntimeError):
                    await request.fulfill(status=404, content_type='text/plain', body='Asset is unavailable'); return
                await request.fulfill(status=200, body=content if request.request.method == 'GET' else b'', headers={
                    'content-type': info['mime'], 'cache-control': 'no-store', 'x-content-type-options': 'nosniff',
                    'content-security-policy': CSP})
            else:
                await request.continue_()
        await context.route('**/*', route)
        page = await context.new_page()
        problems = []
        def problem(value):
            if len(problems) < 10: problems.append(str(value)[:500])
        page.on('console', lambda message: problem(message.text) if message.type == 'error' else None)
        page.on('pageerror', problem)
        response = await page.goto(target, wait_until='load', timeout=25000)
        if response and response.status >= 400: raise RuntimeError('Page returned HTTP ' + str(response.status))
        await page.evaluate('Promise.race([document.fonts.ready.then(()=>true),new Promise(r=>setTimeout(r,1000))])')
        await page.wait_for_timeout(wait_ms)
        size = await page.evaluate('({width:Math.max(innerWidth,document.documentElement.scrollWidth),height:Math.max(innerHeight,document.documentElement.scrollHeight)})')
        truncated = bool(full_page and (size['height'] > 8192 or size['width'] > 4096 or size['width'] * size['height'] > 16000000))
        data = await page.screenshot(full_page=full_page and not truncated, timeout=10000)
        return data, {'url': str(file_path) if file_path else page.url, 'title': await page.title(),
            'truncated': truncated, 'console_errors': problems, 'backend': 'playwright-hidden'}
    finally:
        await context.close()
        manager._render_contexts.discard(context)
