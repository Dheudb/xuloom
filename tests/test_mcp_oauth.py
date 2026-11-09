"""Actual SDK, PKCE, HTTP and encrypted receipts; only owned loopback servers."""
import asyncio
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

try:
    import httpx
    from xuloom.protocols.mcp_oauth import MCPAuth, login, logout
    from xuloom.protocols.oauth_callback import OAuthCallback
    from xuloom.protocols.oauth_store import OAuthFailure, OAuthRequired, OAuthStore
    AVAILABLE=True
except ImportError:
    AVAILABLE=False

from xuloom.core import config
from xuloom.protocols.mcp_config import MCPConfigStore, validate_server


class OAuthService:
    def __init__(self):
        self.calls=[];self.clients={};self.codes={};self.tokens=set();self.refreshes=set();self.counter=0
        self.rotate=True;self.bad_issuer=False;self.reject_refresh=False;self.refreshed=0
        self.resource_tokens=[];self.persistent=False;self.hold=threading.Event()
        self.mcp_mode=False
        self.empty_access=False
        self.client_methods=["none","client_secret_basic","client_secret_post"]
        self.extra_issuers=[]
        self.redirect_resource=False;self.redirect_token=False;self.public_get_disabled=False
        owned=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version="HTTP/1.1"
            def log_message(self,*args): pass
            def reply(self,value,status=200,headers=None):
                data=json.dumps(value).encode()
                self.send_response(status)
                for key,value in (headers or {}).items(): self.send_header(key,value)
                self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(data)))
                self.end_headers();self.wfile.write(data)
            def do_GET(self):
                parsed=urlsplit(self.path)
                owned.calls.append((self.server.kind,"GET",parsed.path,self.headers.get("Authorization")))
                if self.server.kind=="resource":
                    if parsed.path=="/metadata" or parsed.path.startswith("/.well-known/oauth-protected-resource"): return self.reply({"resource":owned.url,"authorization_servers":[*owned.extra_issuers,owned.issuer]})
                    if owned.redirect_resource and parsed.path=="/mcp":return self.reply({},307,{"Location":"/mcp/"})
                    if owned.public_get_disabled:return self.reply({},405)
                    token=self.headers.get("Authorization","").removeprefix("Bearer ")
                    owned.resource_tokens.append(token)
                    if token not in owned.tokens:
                        return self.reply({},401,{"WWW-Authenticate":'Bearer resource_metadata="'+owned.base+'/metadata"'})
                    if owned.mcp_mode: return self.reply({},405)
                    if owned.persistent:
                        self.send_response(200);self.send_header("Content-Type","text/event-stream");self.end_headers();self.wfile.flush()
                        owned.hold.wait(5);self.close_connection=True;return
                    return self.reply({"ok":True})
                if parsed.path.startswith("/.well-known/"):
                    return self.reply({"issuer":owned.issuer,"authorization_endpoint":owned.issuer+"/authorize",
                        "token_endpoint":owned.issuer+"/token","registration_endpoint":owned.issuer+"/register","revocation_endpoint":owned.issuer+"/revoke",
                        "code_challenge_methods_supported":["S256"],"token_endpoint_auth_methods_supported":owned.client_methods,"authorization_response_iss_parameter_supported":True})
                if parsed.path=="/authorize":
                    q=parse_qs(parsed.query)
                    assert q["client_id"][0] in owned.clients
                    assert q["resource"]==[owned.url]
                    assert q["code_challenge_method"]==["S256"]
                    code="owned-code-"+str(len(owned.codes));owned.codes[code]=q
                    redirect=q["redirect_uri"][0]+"?"+urlencode({"code":code,"state":q["state"][0],"iss":"http://wrong.invalid" if owned.bad_issuer else owned.issuer})
                    return self.reply({},302,{"Location":redirect})
                return self.reply({},404)
            def do_POST(self):
                data=self.rfile.read(int(self.headers.get("Content-Length","0")))
                path=urlsplit(self.path).path
                owned.calls.append((self.server.kind,"POST",path,self.headers.get("Authorization")))
                if self.server.kind=="resource":
                    token=self.headers.get("Authorization","").removeprefix("Bearer ")
                    if token not in owned.tokens:return self.reply({},401,{"WWW-Authenticate":'Bearer resource_metadata="'+owned.base+'/metadata"'})
                    request=json.loads(data)
                    if "id" not in request:return self.reply({},202)
                    result={"protocolVersion":request.get("params",{}).get("protocolVersion","2025-11-25"),"capabilities":{"tools":{}},"serverInfo":{"name":"owned-oauth","version":"1"}} if request["method"]=="initialize" else {"tools":[]}
                    return self.reply({"jsonrpc":"2.0","id":request["id"],"result":result})
                if path=="/register":
                    value=json.loads(data);client="owned-client-"+str(len(owned.clients));owned.clients[client]=value
                    return self.reply({**value,"client_id":client},201)
                q=parse_qs(data.decode())
                if owned.redirect_token and path=="/token":return self.reply({},307,{"Location":"/token-canonical"})
                if path in {"/token","/token-canonical","/revoke"}:
                    client=owned.clients.get(q.get("client_id",[""])[0],{})
                    secret=client.get("client_secret")
                    method=client.get("token_endpoint_auth_method","none")
                    if secret:
                        if method=="client_secret_basic":
                            actual=self.headers.get("Authorization","")
                            expected="Basic "+base64.b64encode((urlencode({"k":q["client_id"][0]})[2:].replace("+","%20")+":"+urlencode({"k":secret})[2:].replace("+","%20")).encode()).decode()
                            if actual!=expected or "client_secret" in q:return self.reply({"error":"invalid_client"},401)
                        elif method=="client_secret_post":
                            if q.get("client_secret")!=[secret] or self.headers.get("Authorization"):return self.reply({"error":"invalid_client"},401)
                if path in {"/token","/token-canonical"}:
                    if q["grant_type"]==["authorization_code"]:
                        request=owned.codes.pop(q["code"][0])
                        challenge=base64.urlsafe_b64encode(hashlib.sha256(q["code_verifier"][0].encode()).digest()).decode().rstrip("=")
                        assert challenge==request["code_challenge"][0]
                        assert q["redirect_uri"]==request["redirect_uri"]
                        assert q["client_id"]==request["client_id"]
                        assert q["resource"]==[owned.url]
                    else:
                        assert q["resource"]==[owned.url]
                        if owned.reject_refresh or q["refresh_token"][0] not in owned.refreshes:
                            return self.reply({"error":"invalid_grant"},400)
                        owned.refreshed+=1
                        if owned.rotate: owned.refreshes.remove(q["refresh_token"][0])
                    owned.counter+=1;token="owned-access-"+str(owned.counter);refresh="owned-refresh-"+str(owned.counter)
                    owned.tokens.add(token)
                    result={"access_token":"" if owned.empty_access else token,"token_type":"Bearer","expires_in":60}
                    if owned.rotate or q["grant_type"]==["authorization_code"]:
                        owned.refreshes.add(refresh);result["refresh_token"]=refresh
                    return self.reply(result)
                if path=="/revoke":
                    owned.tokens.discard(q["token"][0]);owned.refreshes.discard(q["token"][0]);return self.reply({})
                return self.reply({},404)
        self.servers=[]
        for kind in ("resource","issuer"):
            server=ThreadingHTTPServer(("127.0.0.1",0),Handler);server.daemon_threads=True;server.kind=kind
            self.servers.append(server);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.base="http://127.0.0.1:"+str(self.servers[0].server_port)
        self.url=self.base+"/mcp";self.issuer="http://127.0.0.1:"+str(self.servers[1].server_port)
    def close(self):
        self.hold.set()
        for server in self.servers:server.shutdown();server.server_close()


@unittest.skipUnless(AVAILABLE and os.name=="nt","MCP SDK and native credential service; POSIX native gate is separate")
class MCPOAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="xuloom-oauth-test-")
        self.root=Path(self.temp.name)
        self.environment=patch.dict(os.environ,{"XULOOM_DATA_DIR":str(self.root/"data"),"XULOOM_CREDENTIAL_STORAGE":"auto","XULOOM_SKILLS_PATHS":""})
        self.environment.start()
        self.config_patch=patch.object(config,"CONFIG_FILE",self.root/"config"/"config.json");self.config_patch.start()
        self.service=OAuthService()
        self.settings=MCPConfigStore(self.root/"workspace"/".xuloom"/"mcp_servers.json")
        self.settings.save("owned",{"type":"http","url":self.service.url,"auth":{"type":"oauth"}})
        self.raw=validate_server(self.settings.read()["mcpServers"]["owned"])
        self.callback=OAuthCallback()
    async def asyncTearDown(self):
        await self.callback.close()
        await asyncio.to_thread(self.service.close)
        self.config_patch.stop();self.environment.stop();self.temp.cleanup()
    def store(self):return OAuthStore(self.settings,"owned",self.raw)
    async def browser(self,event):
        async with httpx.AsyncClient(follow_redirects=False,trust_env=False) as browser:
            response=await browser.get(event["url"])
            callback=response.headers["Location"]
            result=await browser.get(callback)
            self.assertEqual(result.status_code,200)
    async def authorize(self):
        store=self.store()
        self.assertEqual(await login(store,self.callback,self.browser),{"status":"authorized"})
        return store
    async def call(self,store=None):
        async with httpx.AsyncClient(auth=MCPAuth(store or self.store()),trust_env=False,timeout=10) as client:
            return await client.get(self.service.url)

    async def registered(self, secret=None, methods=None):
        callback=OAuthCallback();uri=await callback.start();await callback.close()
        self.service.client_methods=methods or ["none","client_secret_basic","client_secret_post"]
        method="none" if secret is None else "client_secret_basic" if "client_secret_basic" in self.service.client_methods else "client_secret_post"
        self.service.clients["owned-fixed-client"]={"redirect_uris":[uri],"client_secret":secret,"token_endpoint_auth_method":method}
        value={**self.raw,"auth":{"type":"oauth","client_id":"owned-fixed-client","issuer":self.service.issuer,"redirect_uri":uri}}
        if secret is not None:value["auth"]["client_secret"]=secret
        await asyncio.to_thread(self.settings.save,"owned",value,existing=True)
        self.raw=validate_server(self.settings.read()["mcpServers"]["owned"])
        return uri

    async def test_registered_public_client_uses_exact_callback_without_registration(self):
        uri=await self.registered()
        async def browser(event):
            self.assertEqual(parse_qs(urlsplit(event["url"]).query)["redirect_uri"],[uri])
            await self.browser(event)
        await login(self.store(),self.callback,browser)
        self.assertEqual((await self.call()).status_code,200)
        self.assertFalse(any(path=="/register" for _,_,path,_ in self.service.calls))

    async def test_registered_safe_token_redirect_and_resource_slash_do_not_restart_browser(self):
        await self.registered("owned-secret")
        self.service.redirect_resource=True;self.service.redirect_token=True
        prompts=[]
        async def browser(event):prompts.append(event);await self.browser(event)
        store=self.store();await login(store,self.callback,browser)
        self.assertEqual(len(prompts),1)
        self.assertTrue(any(path=="/token-canonical" for _,_,path,_ in self.service.calls))
        self.assertFalse(any(path=="/register" for _,_,path,_ in self.service.calls))

    async def test_disabled_get_discovers_auth_without_creating_an_mcp_session(self):
        self.service.public_get_disabled=True
        await self.authorize()
        self.assertFalse(any(kind=="resource" and method=="POST" for kind,method,_,_ in self.service.calls))

    async def test_registered_secrets_are_encrypted_and_auto_select_token_auth(self):
        secret="owned-secret +:/雪"
        for method in ("client_secret_basic","client_secret_post"):
            with self.subTest(method=method):
                await self.registered(secret,methods=[method])
                self.assertNotIn(secret,self.settings.path.read_text())
                self.assertEqual(self.settings.snapshot()[0]["auth"]["client_secret"],"")
                store=await self.authorize()
                self.assertEqual(store.profile["client"]["token_endpoint_auth_method"],method)
                self.assertNotIn("client_secret",store.profile["client"],"Fixed secrets have one authoritative configuration slot")
                await store.save({**store.profile,"expires_at":time.time()-10})
                self.assertEqual((await self.call()).status_code,200)
                self.assertEqual((await logout(self.store()))["revocation"],"accepted")
        self.assertFalse(any(path=="/register" for _,_,path,_ in self.service.calls))

    async def test_registered_issuer_is_pinned_and_may_be_second_in_resource_metadata(self):
        await self.registered("owned-secret")
        self.service.extra_issuers=["http://127.0.0.1:1"]
        await self.authorize()
        value={**self.raw,"auth":{**self.raw["auth"],"issuer":"https://other-issuer.invalid"}}
        await asyncio.to_thread(self.settings.save,"owned",value,existing=True)
        self.raw=validate_server(self.settings.read()["mcpServers"]["owned"])
        count=len(self.service.calls)
        with self.assertRaises(OAuthFailure):await login(self.store(),OAuthCallback(),self.browser)
        self.assertFalse(any(kind=="issuer" for kind,_,_,_ in self.service.calls[count:]))

    async def test_registered_callback_busy_does_not_choose_new_port_or_register(self):
        uri=await self.registered()
        listener=await asyncio.start_server(lambda r,w:w.close(),"127.0.0.1",urlsplit(uri).port)
        try:
            before=self.settings.read()
            with self.assertRaisesRegex(ValueError,"回调端口不可用"):await login(self.store(),self.callback,self.browser)
            self.assertEqual(self.settings.read(),before)
            self.assertFalse(self.service.calls)
        finally:listener.close();await listener.wait_closed()

    async def test_secret_preserve_clear_and_cross_issuer_copy_guard(self):
        await self.registered("owned-secret")
        value={**self.raw,"auth":{**self.raw["auth"],"client_secret":""}}
        self.settings.save("owned",value,existing=True)
        self.assertEqual(self.settings.read()["mcpServers"]["owned"]["auth"]["client_secret"],"owned-secret")
        original=self.settings.path.read_bytes()
        value["auth"]["issuer"]="https://other-issuer.invalid"
        with self.assertRaisesRegex(ValueError,"re-enter or explicitly clear"):self.settings.save("owned",value,existing=True)
        self.assertEqual(self.settings.path.read_bytes(),original)
        value["auth"]=dict(self.raw["auth"]);value["auth"].pop("client_secret")
        self.settings.save("owned",value,existing=True)
        self.assertNotIn("client_secret",self.settings.read()["mcpServers"]["owned"]["auth"])

    async def test_secret_cannot_be_saved_with_plaintext_storage(self):
        await self.registered("owned-secret")
        original=self.settings.path.read_bytes()
        with patch("xuloom.security.credentials.protection_mode",return_value="file-only"):
            with self.assertRaisesRegex(ValueError,"native credential"):self.settings.save("owned",self.raw,existing=True)
        self.assertEqual(self.settings.path.read_bytes(),original)

    async def test_unreadable_client_secret_blocks_network_and_can_be_replaced(self):
        await self.registered("owned-secret")
        raw=json.loads(self.settings.path.read_text())
        raw["mcpServers"]["owned"]["auth"]["client_secret"]["data"]="corrupt-cipher"
        self.settings.path.write_text(json.dumps(raw))
        self.raw=validate_server(self.settings.read()["mcpServers"]["owned"])
        snapshot=self.settings.snapshot()[0]
        self.assertEqual(snapshot["auth"]["client_secret"],"")
        self.assertIn("凭据不可用",snapshot["error"])
        with self.assertRaises(ValueError):await login(self.store(),self.callback,self.browser)
        self.assertFalse(self.service.calls)
        value={**self.raw,"auth":{**self.raw["auth"],"client_secret":"owned-new-secret"}}
        self.settings.save("owned",value,existing=True)
        self.raw=validate_server(self.settings.read()["mcpServers"]["owned"])
        self.service.clients["owned-fixed-client"]["client_secret"]="owned-new-secret"
        await self.authorize()

    async def test_unsupported_registered_client_auth_does_not_fallback_or_send_secret(self):
        await self.registered("owned-secret")
        for methods in (["private_key_jwt"],[]):
            self.service.client_methods=methods
            with self.assertRaises(OAuthFailure):await login(self.store(),self.callback,self.browser)
        self.assertFalse(any(method=="POST" for _,method,_,_ in self.service.calls))

    async def test_formal_cli_fixed_client_config_and_secret_rotation_never_echo_secret(self):
        uri=await self.registered("owned-secret")
        self.settings.save("owned",{**self.raw,"label":"Owned client title"},existing=True)
        env={**os.environ,"XULOOM_CONFIG_DIR":str(self.root/"config"),"HOME":str(self.root/"home"),"USERPROFILE":str(self.root/"home"),"XULOOM_OWNED_SECRET":"owned-rotated-client-secret"}
        command=[sys.executable,"-m","xuloom","mcp","update","owned","--cwd",str(self.settings.path.parent.parent),"--url",self.service.url,"--oauth","--client-id","owned-fixed-client","--issuer",self.service.issuer,"--redirect-uri",uri,"--client-secret-env","XULOOM_OWNED_SECRET","--json"]
        result=await asyncio.to_thread(subprocess.run,command,env=env,cwd=str(Path(__file__).resolve().parents[1]),capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn(env["XULOOM_OWNED_SECRET"],result.stdout+result.stderr)
        final=json.loads(result.stdout)["result"]
        self.assertEqual(final["servers"][0]["auth"]["client_secret"],"")
        self.assertEqual(final["servers"][0]["label"],"Owned client title")
        self.assertEqual(self.settings.read()["mcpServers"]["owned"]["auth"]["client_secret"],env["XULOOM_OWNED_SECRET"])
        self.assertFalse(self.service.calls)

    async def test_pkce_durable_binding_restart_and_revocation(self):
        store=await self.authorize()
        self.assertNotIn("owned-access",store.path.read_text())
        self.assertNotIn("owned-refresh",store.path.read_text())
        self.assertNotIn("token_endpoint",store.path.read_text())
        self.assertEqual((await self.call()).status_code,200)
        self.assertTrue(all(not auth for kind,_,_,auth in self.service.calls if kind=="issuer"))
        result=await logout(self.store())
        self.assertEqual(result,{"status":"signed_out","revocation":"accepted"})
        count=len(self.service.calls)
        with self.assertRaises(OAuthRequired):await self.call()
        self.assertEqual(len(self.service.calls),count,"Background sign-in must not send discovery or DCR requests")

    async def test_expiry_persists_and_concurrent_refresh_is_single_and_rotates(self):
        store=await self.authorize()
        await store.save({**store.profile,"expires_at":time.time()-10})
        responses=await asyncio.gather(*(self.call() for _ in range(5)))
        self.assertTrue(all(r.status_code==200 for r in responses))
        self.assertEqual(self.service.refreshed,1)
        self.assertEqual((await self.store().load())["tokens"]["refresh_token"],"owned-refresh-2")
        self.assertFalse(any(path=="/token" and kind=="resource" for kind,_,path,_ in self.service.calls))

    async def test_refresh_omission_keeps_previous_refresh_and_invalid_grant_stops(self):
        store=await self.authorize();self.service.rotate=False
        await store.save({**store.profile,"expires_at":time.time()-10})
        await self.call()
        store=self.store();profile=await store.load()
        self.assertEqual(profile["tokens"]["refresh_token"],"owned-refresh-1")
        await store.save({**profile,"expires_at":time.time()-10});self.service.reject_refresh=True
        with self.assertRaises(OAuthRequired):await self.call()
        count=len(self.service.calls)
        with self.assertRaises(OAuthRequired):await self.call()
        self.assertEqual(len(self.service.calls),count)

    async def test_401_refreshes_once_and_never_background_registers(self):
        await self.authorize();self.service.tokens.clear()
        self.assertEqual((await self.call()).status_code,200)
        self.assertEqual(self.service.refreshed,1)
        self.assertEqual(len(self.service.clients),1)

    async def test_sign_out_during_browser_wait_cannot_resurrect_tokens(self):
        pending=asyncio.Event();release=asyncio.Event()
        async def browser(event):
            pending.set();await release.wait();await self.browser(event)
        task=asyncio.create_task(login(self.store(),self.callback,browser))
        await asyncio.wait_for(pending.wait(),10)
        await self.store().forget();release.set()
        with self.assertRaises(OAuthRequired):await task
        self.assertEqual(self.store().snapshot()["status"],"signed_out")

    async def test_changed_target_never_receives_old_bearer_or_refresh_token(self):
        await self.authorize()
        self.settings.save("owned",{**self.raw,"url":self.service.base+"/other"},existing=True)
        count=len(self.service.calls)
        with self.assertRaises(OAuthRequired):await self.call()
        self.assertEqual(len(self.service.calls),count)

    async def test_cancelled_disk_write_cannot_overwrite_a_later_logout(self):
        store=await self.authorize()
        from xuloom.protocols import oauth_store
        atomic=oauth_store.atomic_json
        entered=threading.Event();release=threading.Event()
        def slow(path,value):
            if value.get("oauth_state") is not None:
                entered.set();release.wait(5)
            return atomic(path,value)
        with patch.object(oauth_store,"atomic_json",side_effect=slow):
            task=asyncio.create_task(store.save(store.profile))
            self.assertTrue(await asyncio.to_thread(entered.wait,5))
            task.cancel()
            forget=asyncio.create_task(self.store().forget())
            await asyncio.sleep(.1);self.assertFalse(forget.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):await task
            await forget
        self.assertEqual(self.store().snapshot()["status"],"signed_out")

    async def test_remove_and_readd_cannot_restore_an_old_account(self):
        await self.authorize()
        await asyncio.to_thread(self.settings.remove,"owned")
        await asyncio.to_thread(self.settings.save,"owned",self.raw)
        self.assertEqual(self.store().snapshot()["status"],"signed_out")

    async def test_persistence_failure_keeps_old_account_and_redacts_sdk_diagnostics(self):
        store=await self.authorize();original=store.path.read_bytes()
        callback=OAuthCallback()
        with patch("xuloom.protocols.oauth_store.atomic_json",side_effect=OSError("owned-sensitive-do-not-log")):
            with self.assertLogs("mcp.client.auth.oauth2",level="DEBUG") as logs:
                with self.assertRaises(OAuthFailure):await login(self.store(),callback,self.browser)
        self.assertEqual(store.path.read_bytes(),original)
        self.assertNotIn("owned-sensitive-do-not-log","\n".join(logs.output))

    async def test_empty_access_token_never_publishes_success_or_replaces_previous_account(self):
        self.service.empty_access=True
        with self.assertRaises(OAuthFailure):await login(self.store(),self.callback,self.browser)
        self.assertEqual(self.store().snapshot()["status"],"signed_out")
        self.service.empty_access=False
        store=await self.authorize();original=store.path.read_bytes()
        self.service.empty_access=True
        with self.assertRaises(OAuthFailure):await login(self.store(),OAuthCallback(),self.browser)
        self.assertEqual(store.path.read_bytes(),original)

    async def test_unprotected_storage_is_rejected_before_network_or_callback(self):
        with patch("xuloom.security.credentials.protection_mode",return_value="file-only"):
            with self.assertRaises(OAuthFailure):await login(self.store(),self.callback,self.browser)
        self.assertFalse(self.service.calls)
        self.assertIsNone(self.callback.server)
        self.assertEqual((await logout(self.store()))["revocation"],"not_needed")

    async def test_explicit_signin_repairs_unreadable_cipher_and_logout_recovers_corrupt_receipt(self):
        store=await self.authorize()
        raw=json.loads(store.path.read_text());raw["oauth_state"]["data"]="invalid-ciphertext"
        store.path.write_text(json.dumps(raw))
        count=len(self.service.calls)
        with self.assertRaises(OAuthRequired):await self.call()
        self.assertEqual(len(self.service.calls),count)
        await login(self.store(),OAuthCallback(),self.browser)
        self.assertEqual(self.store().snapshot()["status"],"authorized")
        store.path.write_text("{broken-owned-receipt")
        result=await logout(self.store());self.assertEqual(result["status"],"signed_out")
        self.assertEqual(self.store().snapshot()["status"],"signed_out")
        self.assertEqual(len(list(store.directory.glob(store.identity+".invalid-*.json"))),1)

    async def test_bad_issuer_callback_is_rejected_before_code_exchange(self):
        self.service.bad_issuer=True
        async def browser(event):
            async with httpx.AsyncClient(follow_redirects=False,trust_env=False) as client:
                response=await client.get(event["url"])
                uri=response.headers["Location"]
                self.assertEqual((await client.get(uri)).status_code,400)
                self.assertFalse(any(path=="/token" for _,_,path,_ in self.service.calls))
                uri=uri.replace("iss=http%3A%2F%2Fwrong.invalid","iss="+urlencode({"v":self.service.issuer})[2:])
                self.assertEqual((await client.get(uri)).status_code,200)
        await login(self.store(),self.callback,browser)

    async def test_formal_cli_signin_and_fresh_process_refresh_use_same_receipt(self):
        env={**os.environ,"XULOOM_CONFIG_DIR":str(self.root/"config"),"HOME":str(self.root/"home"),"USERPROFILE":str(self.root/"home")}
        cwd=str(Path(__file__).resolve().parents[1])
        process=subprocess.Popen([sys.executable,"-m","xuloom","mcp","login","owned","--cwd",str(self.settings.path.parent.parent),"--no-browser","--json"],
            env=env,cwd=cwd,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8")
        try:
            line=await asyncio.wait_for(asyncio.to_thread(process.stdout.readline),15)
            event=json.loads(line);self.assertEqual(event["type"],"mcp_oauth_authorize")
            await self.browser(event)
            output,error=await asyncio.wait_for(asyncio.to_thread(process.communicate),15)
            self.assertEqual(process.returncode,0,error)
            self.assertTrue(json.loads(output)["result"]["ok"])
            store=self.store();profile=await store.load();await store.save({**profile,"expires_at":time.time()-10})
            code="""import asyncio,sys,httpx
from xuloom.protocols.mcp_config import MCPConfigStore
from xuloom.protocols.oauth_store import OAuthStore
from xuloom.protocols.mcp_oauth import MCPAuth
async def main():
 s=MCPConfigStore(sys.argv[1]);config=s.read()['mcpServers']['owned']
 async with httpx.AsyncClient(auth=MCPAuth(OAuthStore(s,'owned',config)),trust_env=False) as client:
  response=await client.get(config['url']);assert response.status_code==200
 print('OWNED_REFRESH_OK')
asyncio.run(main())
"""
            result=await asyncio.to_thread(subprocess.run,[sys.executable,"-c",code,str(self.settings.path)],env=env,cwd=cwd,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),"OWNED_REFRESH_OK")
            self.assertEqual(self.service.refreshed,1)
        finally:
            if process.poll() is None:process.kill();await asyncio.to_thread(process.wait,timeout=5)
            process.stdout.close();process.stderr.close()

    async def test_successful_login_does_not_wait_for_persistent_event_stream(self):
        self.service.persistent=True
        await asyncio.wait_for(self.authorize(),4)

    async def test_real_mcp_transport_requires_login_then_uses_cached_auth(self):
        from xuloom.protocols.mcp_bridge import MCPBridge
        self.service.mcp_mode=True
        bridge=MCPBridge(self.settings.path)
        try:
            await bridge.load_and_connect(only="owned")
            self.assertIn("需要登录",bridge.statuses["owned"]["error"])
            self.assertFalse(self.service.calls,"No background discovery before explicit sign-in")
        finally:await bridge.cleanup()
        await self.authorize()
        bridge=MCPBridge(self.settings.path)
        try:
            await bridge.load_and_connect(only="owned")
            self.assertEqual(bridge.statuses["owned"]["status"],"connected")
        finally:await bridge.cleanup()

    async def test_desktop_reconnect_stamp_detects_cli_authorization_without_decrypting_tokens(self):
        from xuloom.desktop.bridge import DesktopBridge
        bridge=DesktopBridge(lambda event:None,self.settings.path.parent.parent)
        before=bridge.config_stamp()
        await self.authorize()
        with patch("xuloom.security.credentials.native",side_effect=AssertionError("Stamp must not decrypt credentials")):
            after=bridge.config_stamp()
        self.assertNotEqual(before,after)
        await self.store().forget()
        self.assertNotEqual(after,bridge.config_stamp())


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_cli_empty_registered_options_cannot_silently_choose_dynamic_registration(self):
        with tempfile.TemporaryDirectory(prefix="xuloom-client-invalid-") as directory:
            root=Path(directory)
            env={**os.environ,"HOME":str(root/"home"),"USERPROFILE":str(root/"home"),"XULOOM_CONFIG_DIR":str(root/"config"),"XULOOM_DATA_DIR":str(root/"data"),"XULOOM_SKILLS_PATHS":""}
            for options in [["--client-id","","--issuer","","--redirect-uri",""],["--client-secret-env",""]]:
                result=subprocess.run([sys.executable,"-m","xuloom","mcp","add","owned","--cwd",str(root/"project"),"--url","https://resource.example/mcp","--oauth",*options,"--json"],
                    env=env,cwd=str(Path(__file__).resolve().parents[1]),capture_output=True,text=True,encoding='utf-8',timeout=20)
                self.assertNotEqual(result.returncode,0)
                self.assertFalse(json.loads(result.stdout)["result"]["ok"])
                self.assertFalse((root/"project"/".xuloom"/"mcp_servers.json").exists())

    def test_registered_configuration_is_strict_and_does_not_silently_mix_methods(self):
        auth={"type":"oauth","client_id":"public-client","issuer":"https://issuer.example","redirect_uri":"http://127.0.0.1:12345/callback"}
        value={"type":"http","url":"https://resource.example/mcp","auth":auth}
        self.assertEqual(validate_server(value)["auth"]["client_id"],"public-client")
        for uri in ["http://127.0.0.1:12345//callback","http://127.0.0.1:12345/a/../callback","http://127.0.0.1:12345/%2e%2e/callback","http://127.0.0.1:12345/%5cexample"]:
            with self.assertRaises(ValueError):validate_server({**value,"auth":{**auth,"redirect_uri":uri}})
        for field,bad in [("client_id",[]),("issuer",{}),("issuer","https://issuer.example:bad"),("issuer","http://issuer.example"),("redirect_uri","http://localhost:12345/callback"),("redirect_uri","http://127.0.0.1:12345/call back"),("redirect_uri","http://127.0.0.1:12345/回调"),("redirect_uri","http://127.0.0.1:12345/callback?key=x"),("client_secret",""),("client_secret","x\ny"),("token_endpoint_auth_method",[]),("token_endpoint_auth_method","client_secret_post"),("client_metadata_url","https://client.example/metadata.json")]:
            with self.subTest(field=field,bad=bad):
                with self.assertRaises(ValueError):validate_server({**value,"auth":{**auth,field:bad}})

    async def test_wrong_state_host_and_issuer_do_not_consume_callback(self):
        if not AVAILABLE:self.skipTest("MCP SDK")
        callback=OAuthCallback();await callback.start()
        self.addAsyncCleanup(callback.close)
        callback.state="owned-state";callback.issuer="https://issuer.example";callback.require_issuer=True
        for query in ["state=wrong&code=one&iss=https%3A%2F%2Fissuer.example", "state=owned-state&code=one", "state=owned-state&code=one&iss=https%3A%2F%2Fwrong.example"]:
            self.assertFalse(callback.accept(callback.url+"?"+query))
            self.assertFalse(callback.future.done())
        valid=callback.url+"?"+urlencode({"state":"owned-state","code":"owned-code","iss":"https://issuer.example"})
        async with httpx.AsyncClient(trust_env=False) as client:
            self.assertEqual((await client.get(valid,headers={"Host":"attacker.example"})).status_code,400)
            self.assertFalse(callback.future.done())
            self.assertEqual((await client.get(valid)).status_code,200)
        self.assertEqual(await callback.future,("owned-code","owned-state"))

    def test_oauth_configuration_cannot_mix_credentials_or_stdio(self):
        for value in [
            {"type":"stdio","command":"owned","auth":{"type":"oauth"}},
            {"type":"http","url":"https://mcp.example","headers":{"Authorization":"Bearer owned"},"auth":{"type":"oauth"}},
            {"type":"http","url":"https://mcp.example?key=owned","auth":{"type":"oauth"}},
        ]:
            with self.assertRaises(ValueError):validate_server(value)
