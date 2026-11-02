"""Owned loopback callback; callback codes never become chat messages."""
import asyncio
import secrets
from urllib.parse import parse_qs, urlsplit

from xuloom.protocols.oauth_store import OAuthFailure


class OAuthCallback:
    def __init__(self):
        self.server=None
        self.url=""
        self.state=None
        self.issuer=None
        self.require_issuer=False
        self.future=None
        self.clients=set()

    async def start(self, previous=None):
        parsed=urlsplit(previous) if previous else None
        if parsed and (parsed.scheme!="http" or parsed.hostname!="127.0.0.1" or not parsed.port or
                       parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise OAuthFailure()
        self.future=asyncio.get_running_loop().create_future()
        self.server=await asyncio.start_server(self.handle,"127.0.0.1",parsed.port if parsed else 0,limit=8192)
        port=self.server.sockets[0].getsockname()[1]
        self.url="http://127.0.0.1:"+str(port)+(parsed.path if parsed else "/oauth/"+secrets.token_urlsafe(24))
        return self.url

    def accept(self, url):
        if not self.future or self.future.done() or not self.state: return False
        try:
            if not isinstance(url,str) or len(url)>8192: return False
            parsed,expected=urlsplit(url),urlsplit(self.url)
            if (parsed.scheme,parsed.netloc,parsed.path)!=(expected.scheme,expected.netloc,expected.path) or parsed.fragment:
                return False
            values=parse_qs(parsed.query,keep_blank_values=True,strict_parsing=True,max_num_fields=12)
            if any(len(v)!=1 for v in values.values()): return False
            state=values.get("state",[""])[0]
            if not secrets.compare_digest(state,self.state): return False
            issuer=values.get("iss",[None])[0]
            if (self.require_issuer and issuer is None) or issuer is not None and issuer!=self.issuer: return False
            if "error" in values:
                self.future.set_exception(OAuthFailure())
            elif values.get("code",[""])[0] and len(values["code"][0])<=4096:
                self.future.set_result((values["code"][0],state))
            else: return False
            return True
        except (ValueError,TypeError):
            return False

    async def handle(self, reader, writer):
        task=asyncio.current_task();self.clients.add(task)
        status="400 Bad Request"
        try:
            raw=await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"),5)
            lines=raw.decode("ascii").split("\r\n")
            method,target,version=lines[0].split(" ")
            hosts=[line.split(":",1)[1].strip() for line in lines[1:] if line.lower().startswith("host:")]
            if method=="GET" and version=="HTTP/1.1" and hosts==[urlsplit(self.url).netloc] and target.startswith("/") and not target.startswith("//"):
                parsed=urlsplit(target)
                if not parsed.scheme and not parsed.netloc and self.accept("http://"+hosts[0]+target): status="200 OK"
            body=b"Authorization received. You can return to Xuloom." if status=="200 OK" else b"Callback rejected. Return to Xuloom to retry or cancel."
            writer.write(("HTTP/1.1 "+status+"\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\nContent-Security-Policy: default-src 'none'\r\nConnection: close\r\nContent-Length: "+str(len(body))+"\r\n\r\n").encode()+body)
            await writer.drain()
        except (ValueError,UnicodeError,OSError,asyncio.TimeoutError,asyncio.IncompleteReadError,asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
            try: await writer.wait_closed()
            except OSError: pass
            self.clients.discard(task)

    async def close(self):
        if self.server:
            self.server.close();await self.server.wait_closed()
        if self.future and not self.future.done(): self.future.cancel()
        elif self.future and not self.future.cancelled(): self.future.exception()
        for task in list(self.clients): task.cancel()
        await asyncio.gather(*list(self.clients),return_exceptions=True)
