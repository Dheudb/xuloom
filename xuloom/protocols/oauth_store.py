"""Encrypted OAuth receipts, bound to one explicitly configured MCP service."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
import time
import uuid

from xuloom.core.session_lock import SessionBusyError, SessionLock
from xuloom.core.storage import atomic_json, data_directory
from xuloom.security.credentials import is_protected, protection_mode, transform


class OAuthRequired(ValueError):
    def __init__(self):
        super().__init__("MCP 需要登录或重新授权；请在 MCP 设置中登录。后台不会打开浏览器。")


class OAuthFailure(ValueError):
    def __init__(self):
        super().__init__("MCP OAuth 未完成；检查服务的授权配置，原有凭据未被空值覆盖。")


@asynccontextmanager
async def lease(directory, identity, timeout=10):
    lock=SessionLock(directory,identity)
    deadline=time.monotonic()+timeout
    try:
        while True:
            try:
                lock.acquire()  # Non-blocking OS lease; no orphaned acquisition thread on cancellation.
                break
            except SessionBusyError:
                if time.monotonic()>=deadline:
                    raise ValueError("此 MCP 授权正由另一个进程处理；稍后重试。") from None
                await asyncio.sleep(.05)
        yield
    finally:
        lock.close()


class OAuthStore:
    def __init__(self, store, name, config):
        self.store,self.name=store,name
        self.config=config
        identity=json.dumps([str(store.path.resolve()),name],ensure_ascii=False)
        self.identity=hashlib.sha256(identity.encode()).hexdigest()
        self.directory=data_directory()/"mcp-oauth"
        self.path=self.directory/(self.identity+".json")
        self.binding=self.fingerprint(config)
        self.revision=None
        self.profile={}
        self.repair=False

    @staticmethod
    def fingerprint(config):
        # Toggle/label edits are harmless; target, auth and header changes are not.
        value={key:config.get(key) for key in ("type","url","auth","headers")}
        return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def current(self):
        from xuloom.protocols.mcp_config import validate_server
        raw=self.store.read(include_skills=True)["mcpServers"].get(self.name)
        if raw is None or self.fingerprint(validate_server(raw))!=self.binding:
            raise OAuthRequired()

    def read(self):
        self.current()
        if not self.path.exists(): return {"revision":0,"binding":self.binding},{}
        try:
            raw=json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw,dict) or type(raw.get("revision")) is not int or raw["revision"]<0:
                raise OAuthFailure()
            if raw.get("binding")!=self.binding: return raw,{}
            if raw.get("oauth_state") is None: return raw,{}
            if not is_protected(raw["oauth_state"]): raise OAuthFailure()
            value=transform(raw,self.path)["oauth_state"]
            if not isinstance(value,str):
                if self.repair:return raw,{}
                raise OAuthRequired()
            try:profile=json.loads(value)
            except ValueError:
                if self.repair:return raw,{}
                raise
            if not isinstance(profile,dict): raise OAuthFailure()
            return raw,profile
        except (OSError,ValueError,TypeError,KeyError):
            raise OAuthRequired() from None

    async def load(self):
        raw,self.profile=await asyncio.to_thread(self.read)
        self.revision=raw["revision"]
        return self.profile

    async def check(self):
        raw,_=await asyncio.to_thread(self.read)
        if self.revision is not None and raw["revision"]!=self.revision: raise OAuthRequired()

    async def save(self, profile):
        async with lease(self.directory,self.identity+"-storage"):
            raw,_=await asyncio.to_thread(self.read)
            if self.revision is None or raw["revision"]!=self.revision: raise OAuthRequired()
            if protection_mode() in {"file-only","unavailable"}: raise OAuthFailure()
            document={"revision":self.revision+1,"binding":self.binding,
                "oauth_state":json.dumps(profile,ensure_ascii=False)}
            protected=await asyncio.to_thread(transform,document,self.path,protect=True)
            if not is_protected(protected["oauth_state"]): raise OAuthFailure()
            publishing=asyncio.create_task(asyncio.to_thread(atomic_json,self.path,protected))
            try: await asyncio.shield(publishing)
            except asyncio.CancelledError:
                # Do not release the writer lease while an authorized disk
                # write is still in flight: logout must win after it finishes.
                await publishing
                raise
            self.revision+=1
            self.profile=profile

    async def forget(self):
        # A short storage lease is independent of a browser/refresh flow lease.
        await asyncio.to_thread(self.forget_sync)

    def forget_sync(self, *, check_config=True):
        lock=SessionLock(self.directory,self.identity+"-storage")
        deadline=time.monotonic()+10
        try:
            while True:
                try: lock.acquire();break
                except SessionBusyError:
                    if time.monotonic()>=deadline: raise OAuthFailure() from None
                    time.sleep(.05)
            if check_config: self.current()
            revision=0
            if self.path.exists():
                try:
                    raw=json.loads(self.path.read_text(encoding="utf-8"))
                    revision=raw["revision"]
                    if type(revision) is not int or revision<0: raise ValueError()
                except (OSError,ValueError,TypeError,KeyError):
                    # Explicit logout may repair a corrupt receipt. Move the
                    # existing bytes aside rather than making a plaintext copy.
                    quarantine=self.path.with_name(self.path.stem+".invalid-"+uuid.uuid4().hex+".json")
                    self.path.replace(quarantine)
                    revision=int.from_bytes(os.urandom(16),"big")
            atomic_json(self.path,{"revision":revision+1,"binding":self.binding,"oauth_state":None})
        finally:
            lock.close()

    def snapshot(self):
        try:
            _,profile=self.read()
            return {"status":"authorized" if profile.get("tokens") else "signed_out"}
        except OAuthRequired:
            return {"status":"unavailable"}
