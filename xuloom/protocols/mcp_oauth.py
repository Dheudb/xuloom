"""MCP SDK OAuth with durable receipts and explicit, cancellable sign-in."""
import asyncio
from contextvars import ContextVar
import ipaddress
import logging
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.client.auth.utils import issuers_match, validate_metadata_issuer
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata, OAuthToken, ProtectedResourceMetadata
from mcp.shared._httpx_utils import next_request_within_origin, stream_within_origin

from xuloom.protocols.oauth_store import OAuthFailure, OAuthRequired, lease


_sensitive=ContextVar("xuloom_mcp_oauth_diagnostic",default=False)
class _SafeDiagnostic(logging.Filter):
    def filter(self, record):
        if _sensitive.get():
            record.msg="MCP OAuth diagnostic (sensitive details omitted)"
            record.args=();record.exc_info=None;record.exc_text=None
        return True
logging.getLogger("mcp.client.auth.oauth2").addFilter(_SafeDiagnostic())
logging.getLogger("httpx").addFilter(_SafeDiagnostic())


def secure_url(value):
    parsed=urlsplit(str(value))
    try: local=ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError: local=False
    if (parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password or
            parsed.fragment or parsed.scheme=="http" and not local): raise OAuthFailure()
    return str(value)


async def bounded_response(response):
    chunks=[];size=0
    async def read():
        nonlocal size
        async for chunk in response.aiter_bytes():
            size+=len(chunk)
            if size>1_000_000: raise OAuthFailure()
            chunks.append(chunk)
    try:
        await asyncio.wait_for(read(),20)
        # HTTPX reads intermediate auth responses again before sending the next
        # request. Retain its response object and cache our bounded body, as
        # Response.aread() does; a replacement would leave it StreamConsumed.
        response._content=b"".join(chunks)
        return response
    finally:
        await response.aclose()


class MCPAuth(OAuthClientProvider):
    # Do not buffer a successful, potentially persistent MCP event stream.
    requires_response_body=False

    def __init__(self, store, callback=None, notify=None):
        self.receipt=store
        self.callback=callback
        self.notify=notify
        self.interactive=callback is not None
        self.metadata_documents={}
        self.force_refresh=False
        self.refreshed=False
        self.profile={}
        metadata=OAuthClientMetadata(redirect_uris=[callback.url if callback else "http://127.0.0.1:1/oauth"],
            client_name="Xuloom",token_endpoint_auth_method="none")
        super().__init__(secure_url(store.config["url"]),metadata,self,self.redirect,self.wait_callback,
            client_metadata_url=store.config.get("auth",{}).get("client_metadata_url"))

    async def get_tokens(self):
        return None if self.interactive else OAuthToken.model_validate(self.profile["tokens"]) if self.profile.get("tokens") else None

    async def get_client_info(self):
        configured=self.receipt.config.get("auth",{})
        if configured.get("client_id"):
            from xuloom.security.credentials import require_secret
            secret=configured.get("client_secret")
            return OAuthClientInformationFull(client_id=configured["client_id"],issuer=configured["issuer"],
                client_secret=require_secret(secret) if secret is not None else None,
                redirect_uris=[configured["redirect_uri"]],token_endpoint_auth_method="none" if secret is None else "client_secret_basic")
        return OAuthClientInformationFull.model_validate(self.profile["client"]) if self.profile.get("client") else None

    def _select_authorization_server(self, advertised):
        issuer=self.receipt.config.get("auth",{}).get("issuer")
        if issuer:
            for server in advertised:
                if issuers_match(server,issuer):return server
            raise OAuthFailure()
        return super()._select_authorization_server(advertised)

    def configure_registered_client(self):
        configured=self.receipt.config.get("auth",{})
        if not configured.get("client_id"):return
        metadata=self.context.oauth_metadata
        client=self.context.client_info
        if not metadata or not client or client.client_id!=configured["client_id"]:raise OAuthFailure()
        validate_metadata_issuer(metadata,configured["issuer"])
        requested=configured.get("token_endpoint_auth_method","auto")
        methods=metadata.token_endpoint_auth_methods_supported
        if methods is None:methods=["client_secret_basic"]
        if configured.get("client_secret") is None:
            method="none"
        elif requested=="auto":
            method=next((item for item in ("client_secret_basic","client_secret_post") if item in methods),None)
            if method is None:raise OAuthFailure()
        else:method=requested
        if metadata.token_endpoint_auth_methods_supported is not None and method not in methods:raise OAuthFailure()
        client.token_endpoint_auth_method=method

    async def _perform_authorization(self):
        self.configure_registered_client()
        return await super()._perform_authorization()

    async def set_client_info(self, client):
        # Registration is staged in SDK context until a token exchange succeeds.
        # Cancelling re-login cannot mismatch the previous account's tokens/client.
        pass

    async def set_tokens(self, tokens):
        from xuloom.security.credentials import api_key
        try:
            access=api_key(tokens.access_token)
            if not access or any(char.isspace() for char in access):raise OAuthFailure()
            if tokens.refresh_token is not None and (not tokens.refresh_token or len(tokens.refresh_token)>65536 or any(ord(char)<32 or ord(char)==127 for char in tokens.refresh_token)):
                raise OAuthFailure()
            if tokens.expires_in is not None and tokens.expires_in<0:raise OAuthFailure()
        except (ValueError,TypeError):raise OAuthFailure() from None
        context=self.context
        if not context.oauth_metadata or not context.client_info or not context.auth_server_url: raise OAuthFailure()
        if self.interactive and self.callback.url not in [str(uri) for uri in context.client_info.redirect_uris or []]:raise OAuthFailure()
        validate_metadata_issuer(context.oauth_metadata,context.auth_server_url)
        metadata=context.oauth_metadata.model_dump(mode="json")
        observed=self.metadata_documents.get(str(context.oauth_metadata.issuer))
        if observed:
            metadata["issuer"]=observed["issuer"]
            metadata["authorization_response_iss_parameter_supported"]=observed.get("authorization_response_iss_parameter_supported",False)
        elif self.profile.get("metadata"):
            metadata["issuer"]=self.profile["metadata"]["issuer"]
            metadata["authorization_response_iss_parameter_supported"]=self.profile["metadata"].get("authorization_response_iss_parameter_supported",False)
        client=context.client_info.model_dump(mode="json")
        if self.receipt.config.get("auth",{}).get("client_id"):client.pop("client_secret",None)
        profile={"tokens":tokens.model_dump(mode="json"),"expires_at":context.token_expiry_time,"issued_at":time.time(),
            "client":client,"metadata":metadata,"auth_server":context.auth_server_url,
            "resource":context.protected_resource_metadata.model_dump(mode="json") if context.protected_resource_metadata else None}
        await self.receipt.save(profile)
        self.profile=profile
        self.interactive=False  # Further requests in this explicit login reuse the saved grant, not a second browser prompt.

    async def _initialize(self):
        self.profile=await self.receipt.load()
        try:
            context=self.context
            context.current_tokens=await self.get_tokens()
            context.client_info=await self.get_client_info()
            context.oauth_metadata=OAuthMetadata.model_validate(self.profile["metadata"]) if self.profile.get("metadata") else None
            context.auth_server_url=self.profile.get("auth_server")
            context.protected_resource_metadata=ProtectedResourceMetadata.model_validate(self.profile["resource"]) if self.profile.get("resource") else None
            if context.protected_resource_metadata: await self._validate_resource_match(context.protected_resource_metadata)
            context.token_expiry_time=self.profile.get("expires_at")
            if self.force_refresh or time.time()+60<self.profile.get("issued_at",0): context.token_expiry_time=0.001
            if context.oauth_metadata:
                validate_metadata_issuer(context.oauth_metadata,context.auth_server_url or "")
                secure_url(context.oauth_metadata.token_endpoint)
                self.configure_registered_client()
            if not self.interactive and (not context.current_tokens or not context.oauth_metadata or not context.client_info):
                raise OAuthRequired()
            self._initialized=True
        except (ValueError,TypeError,KeyError): raise OAuthRequired() from None

    async def _handle_refresh_response(self, response):
        if response.status_code!=200:
            if response.status_code in {400,401}:
                await self.receipt.save({**self.profile,"tokens":None})
                raise OAuthRequired()
            raise OAuthFailure()
        try:
            token=OAuthToken.model_validate_json(await response.aread())
            previous=self.context.current_tokens
            if token.refresh_token is None and previous:
                token=token.model_copy(update={"refresh_token":previous.refresh_token})
            self.context.current_tokens=token
            self.context.update_token_expiry(token)
            await self.set_tokens(token)
            self.refreshed=True
            return True
        except (ValueError,TypeError): raise OAuthFailure() from None

    async def redirect(self, url):
        if not self.interactive: raise OAuthRequired()
        secure_url(url)
        metadata=self.context.oauth_metadata
        if not metadata or metadata.code_challenge_methods_supported and "S256" not in metadata.code_challenge_methods_supported:
            raise OAuthFailure()
        values=parse_qs(urlsplit(url).query)
        states=values.get("state",[])
        if len(states)!=1: raise OAuthFailure()
        observed=self.metadata_documents.get(str(metadata.issuer),self.profile.get("metadata",{}))
        self.callback.state=states[0]
        self.callback.issuer=observed.get("issuer",str(metadata.issuer))
        self.callback.require_issuer=observed.get("authorization_response_iss_parameter_supported") is True
        if self.notify: await self.notify({"url":url,"callback":self.callback.url})

    async def wait_callback(self):
        return await asyncio.wait_for(asyncio.shield(self.callback.future),300)

    def resource_request(self, request):
        expected=httpx.URL(self.context.server_url)
        return (request.url.scheme,request.url.host,request.url.port,request.url.path.rstrip("/"),request.url.query)==(
            expected.scheme,expected.host,expected.port,expected.path.rstrip("/"),expected.query)

    async def async_auth_flow(self, request):
        if not self.resource_request(request): raise OAuthFailure()
        async with lease(self.receipt.directory,self.receipt.identity+"-flow"):
            self._initialized=False
            self.force_refresh=False
            self.refreshed=False
            for attempt in range(2):
                flow=super().async_auth_flow(request)
                allowed_follow=None
                diagnostic=_sensitive.set(True)
                try:
                    outgoing=await flow.__anext__()
                    while True:
                        await self.receipt.check()
                        secure_url(outgoing.url)
                        if outgoing is not request:
                            if outgoing.method=="POST":
                                metadata=self.context.oauth_metadata
                                if not metadata: raise OAuthFailure()
                                fixed=self.receipt.config.get("auth",{}).get("client_id")
                                allowed={str(metadata.token_endpoint)}
                                if self.interactive and not fixed and metadata.registration_endpoint: allowed.add(str(metadata.registration_endpoint))
                                if fixed:
                                    self.configure_registered_client()
                                    grant=parse_qs((await outgoing.aread()).decode()).get("grant_type",[])
                                    if grant not in [["authorization_code"],["refresh_token"]]:raise OAuthFailure()
                                if str(outgoing.url) not in allowed and outgoing is not allowed_follow: raise OAuthFailure()
                            if not self.interactive and outgoing.method!="POST": raise OAuthRequired()
                        response=yield outgoing
                        if outgoing is request:
                            if self.interactive and response.status_code==405 and not self.context.current_tokens:
                                # Explicit sign-in may discover public resource metadata
                                # even when GET itself is disabled. Feed only the SDK's
                                # auth discovery a challenge; keep HTTPX's actual 405.
                                # This avoids creating an MCP initialize/session as a probe.
                                await response.aclose();response._content=b""
                                response=httpx.Response(401,headers=response.headers,request=response.request)
                            if not self.interactive and response.status_code in {401,403}:
                                if response.status_code==401 and attempt==0 and not self.refreshed and self.context.can_refresh_token():
                                    await response.aclose()
                                    response._content=b""
                                    self.force_refresh=True;self._initialized=False
                                    request.headers.pop("Authorization",None)
                                    break
                                await response.aclose()
                                raise OAuthRequired()
                        else:
                            response=await bounded_response(response)
                            if response.status_code==200 and outgoing.method=="GET":
                                try:
                                    value=response.json()
                                    if isinstance(value,dict) and "issuer" in value and "token_endpoint" in value:
                                        flag=value.get("authorization_response_iss_parameter_supported")
                                        if flag is not None and type(flag) is not bool:raise OAuthFailure()
                                        key=str(OAuthMetadata.model_validate(value).issuer)
                                        self.metadata_documents[key]=value
                                except OAuthFailure:raise
                                except (ValueError,TypeError): pass
                        allowed_follow=next_request_within_origin(response) if outgoing is not request else None
                        outgoing=await flow.asend(response)
                except StopAsyncIteration:
                    return
                except (OAuthRequired,OAuthFailure): raise
                except asyncio.CancelledError: raise
                except Exception: raise OAuthFailure() from None
                finally:
                    await flow.aclose()
                    _sensitive.reset(diagnostic)


async def login(store, callback, notify):
    from xuloom.security.credentials import is_protected, protection_mode, transform, unavailable_fields, CredentialUnavailable
    if protection_mode() in {"file-only","unavailable"}:raise OAuthFailure()
    if unavailable_fields(store.config):raise CredentialUnavailable()
    preflight=await asyncio.to_thread(transform,{"oauth_state":"{}"},store.path,protect=True)
    if not is_protected(preflight["oauth_state"]):raise OAuthFailure()
    store.repair=True  # Explicit sign-in may replace an unreadable encrypted value after successful authorization.
    if store.config.get("auth",{}).get("client_secret") is not None:
        await asyncio.to_thread(store.store.migrate)
    profile=await store.load()
    client=profile.get("client",{})
    redirects=client.get("redirect_uris",[]) if isinstance(client,dict) else []
    try:
        registered=store.config.get("auth",{}).get("redirect_uri")
        try:await callback.start(registered or (redirects[0] if redirects else None))
        except OSError:raise ValueError("OAuth 回调端口不可用；请关闭占用程序后重试。注册信息及原账号未改变。") from None
        auth=MCPAuth(store,callback,notify)
        host=urlsplit(store.config["url"]).hostname
        initial_revision=store.revision
        async def probe():
            async with httpx.AsyncClient(auth=auth,timeout=30,follow_redirects=False,max_redirects=5,trust_env=host not in {"127.0.0.1","::1"}) as client:
                headers={**store.config.get("headers",{}),"Accept":"application/json, text/event-stream"}
                async with stream_within_origin(client,"GET",store.config["url"],headers=headers) as response:
                    status=response.status_code
                if status not in {200,202,405,406}: raise OAuthFailure()
        await asyncio.wait_for(probe(),330)
        if store.revision==initial_revision or not store.profile.get("tokens"): raise OAuthFailure()
        return {"status":"authorized"}
    finally:
        await callback.close()


async def logout(store):
    try: profile=await store.load()
    except OAuthRequired: profile={}
    await store.forget()
    if not profile.get("tokens"):return {"status":"signed_out","revocation":"not_needed"}
    metadata=profile.get("metadata",{})
    endpoint=metadata.get("revocation_endpoint")
    if not endpoint: return {"status":"signed_out","revocation":"not_supported"}
    try:
        secure_url(endpoint)
        auth=MCPAuth(store)
        auth.profile=profile
        auth.context.client_info=await auth.get_client_info()
        auth.context.oauth_metadata=OAuthMetadata.model_validate(metadata)
        validate_metadata_issuer(auth.context.oauth_metadata,profile["auth_server"])
        auth.configure_registered_client()
        tokens=profile.get("tokens") or {}
        token=tokens.get("refresh_token") or tokens.get("access_token")
        if not token: return {"status":"signed_out","revocation":"not_needed"}
        body,headers=auth.context.prepare_token_auth({"token":token,"token_type_hint":"refresh_token" if tokens.get("refresh_token") else "access_token"},
            {"Content-Type":"application/x-www-form-urlencoded"})
        async with httpx.AsyncClient(timeout=15,follow_redirects=False,trust_env=urlsplit(endpoint).hostname not in {"127.0.0.1","::1"}) as client:
            response=await client.post(endpoint,data=body,headers=headers)
        return {"status":"signed_out","revocation":"accepted" if response.status_code==200 else "failed"}
    except Exception:
        return {"status":"signed_out","revocation":"failed"}
