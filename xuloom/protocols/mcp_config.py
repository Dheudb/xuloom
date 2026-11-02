"""Shared project MCP settings with redacted environment/header snapshots."""
import copy
import ipaddress
import json
import re
import sys
import time
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit, unquote
from xuloom.security.credentials import is_protected, transform, unavailable_fields


def serialized_edit(operation):
    @wraps(operation)
    def edit(store, *args, **kwargs):
        from xuloom.core.session_lock import SessionLock, SessionBusyError
        lease = SessionLock(store.path.resolve().parent, "mcp-config-writer")
        deadline = time.monotonic() + 10
        try:
            while True:
                try:
                    lease.acquire()
                    break
                except SessionBusyError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("MCP 配置正被另一个进程保存，本次未修改。") from None
                    time.sleep(.02)
            return operation(store, *args, **kwargs)
        finally:
            lease.close()
    return edit


def http_url(value):
    """Validate the original spelling: urlsplit silently strips some controls."""
    try:
        if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError()
        parsed = urlsplit(value)
        parsed.port
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or "#" in value or "\\" in parsed.netloc:
            raise ValueError()
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Provide an HTTP(S) URL without embedded credentials, whitespace or a fragment") from None


def validate_server(config):
    if not isinstance(config, dict):
        raise ValueError("MCP configuration must be an object")
    transport = config.get("type", "http" if "url" in config else "stdio")
    if transport == "streamable-http":
        transport = "http"
    if not isinstance(transport, str) or transport not in {"stdio", "http"}:
        raise ValueError("Supported MCP transports: stdio, http (Streamable HTTP)")
    enabled = config.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("enabled must be a JSON boolean (true or false)")
    result = {"type": transport, "enabled": enabled}
    for key in ("label", "catalogId"):
        value = config.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(key + " must be a string")
            if value:
                result[key] = value[:200]
    if transport == "stdio":
        command = config.get("command", "")
        args = config.get("args", [])
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ValueError("Provide an executable in command")
        if not isinstance(args, list) or any(not isinstance(v, str) or "\x00" in v for v in args):
            raise ValueError("args must be a JSON array of strings")
        result.update(command=command.strip(), args=args)
        mapping = "env"
    else:
        url = config.get("url", "")
        http_url(url.get("preview", "https://unavailable.invalid") if is_protected(url) else url)
        result["url"] = url
        mapping = "headers"
    values = config.get(mapping, {})
    if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str) and not is_protected(v) for k, v in values.items()):
        raise ValueError(mapping + " must be a JSON object of strings")
    for key, value in values.items():
        if mapping == "env" and (not key or "=" in key or "\x00" in key):
            raise ValueError("Invalid environment variable name")
        if mapping == "headers" and (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) or isinstance(value, str) and ("\r" in value or "\n" in value)):
            raise ValueError("Invalid HTTP header")
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("MCP values cannot contain NUL")
    timeout = config.get("tool_timeout_sec", 20)
    if type(timeout) not in {int, float} or not 1 <= timeout <= 600:
        raise ValueError("tool_timeout_sec must be between 1 and 600")
    result["tool_timeout_sec"] = timeout
    result[mapping] = dict(values)
    auth=config.get("auth")
    if auth is not None:
        if transport!="http" or not isinstance(auth,dict) or auth.get("type")!="oauth" or set(auth)-{"type","client_metadata_url","client_id","client_secret","issuer","redirect_uri","token_endpoint_auth_method"}:
            raise ValueError("OAuth is supported only for HTTP MCP services")
        parsed=urlsplit(result["url"]) if isinstance(result["url"],str) else None
        if parsed is None or parsed.query or any(key.lower()=="authorization" for key in values):
            raise ValueError("OAuth cannot be combined with an Authorization header or URL query credentials")
        try: local=ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError: local=False
        if parsed.scheme!="https" and not local: raise ValueError("OAuth requires HTTPS, except loopback IP test services")
        result["auth"]={"type":"oauth"}
        if "client_metadata_url" in auth:
            if not isinstance(auth["client_metadata_url"],str) or not auth["client_metadata_url"] or len(auth["client_metadata_url"])>4096:
                raise ValueError("Invalid client metadata URL")
            url=http_url(auth["client_metadata_url"])
            if url.scheme!="https" or url.path in {"","/"}:
                raise ValueError("Client metadata URL must be HTTPS with a non-root path")
            result["auth"]["client_metadata_url"]=auth["client_metadata_url"]
        fixed=any(key in auth for key in ("client_id","client_secret","issuer","redirect_uri","token_endpoint_auth_method"))
        if fixed:
            if auth.get("client_metadata_url"):raise ValueError("Choose either a fixed client or a client metadata document")
            client=auth.get("client_id")
            if not isinstance(client,str) or not 1<=len(client)<=4096 or any(ord(c)<33 or ord(c)==127 for c in client):
                raise ValueError("Provide the registered OAuth Client ID")
            value=auth.get("issuer")
            if not isinstance(value,str) or len(value)>4096 or any(ord(c)<33 or ord(c)==127 for c in value):raise ValueError("Invalid OAuth issuer")
            issuer=http_url(value)
            try:local=ipaddress.ip_address(issuer.hostname).is_loopback
            except ValueError:local=False
            if issuer.scheme not in {"http","https"} or not issuer.hostname or issuer.username or issuer.password or issuer.query or issuer.fragment or issuer.scheme=="http" and not local:
                raise ValueError("Provide the authorization server issuer; HTTPS is required except loopback tests")
            try:
                if not isinstance(auth.get("redirect_uri"),str) or not auth["redirect_uri"].isascii() or any(ord(c)<33 or ord(c)==127 for c in auth["redirect_uri"]):raise ValueError()
                callback=urlsplit(auth.get("redirect_uri",""))
                port=callback.port
                canonical="http://127.0.0.1:"+str(port)+callback.path
                path=unquote(callback.path)
                if callback.scheme!="http" or callback.hostname!="127.0.0.1" or not port or callback.username or callback.password or callback.query or callback.fragment or not callback.path.startswith("/") or path.startswith("//") or "\\" in path or any(part in {".",".."} for part in path.split("/")) or auth["redirect_uri"]!=canonical:
                    raise ValueError()
            except (ValueError,TypeError,AttributeError,KeyError):
                raise ValueError("Registered callback must be an exact http://127.0.0.1:PORT/path URL") from None
            secret=auth.get("client_secret")
            if secret is not None and not is_protected(secret) and (not isinstance(secret,str) or not secret or len(secret)>65536 or any(ord(c)<32 or ord(c)==127 for c in secret)):
                raise ValueError("Invalid OAuth client secret")
            method=auth.get("token_endpoint_auth_method","auto")
            if not isinstance(method,str) or method not in {"auto","none","client_secret_basic","client_secret_post"} or secret is None and method not in {"auto","none"} or secret is not None and method=="none":
                raise ValueError("OAuth client authentication does not match the provided secret")
            result["auth"].update(client_id=client,issuer=auth["issuer"],redirect_uri=auth["redirect_uri"],token_endpoint_auth_method=method)
            if secret is not None:result["auth"]["client_secret"]=secret
    return result


def validate_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,47}", name):
        raise ValueError("MCP name must start with a letter; use up to 48 letters, digits, - or _")


class MCPConfigStore:
    def __init__(self, path):
        self.path = Path(path)

    def skill_servers(self, *, issues=None, manifests=None):
        from xuloom.skills.registry import SkillRegistry
        defaults = {}
        root = self.path.resolve().parent
        if root.name in {".xuloom"}:
            root = root.parent
        for skill in SkillRegistry(root).active():
            manifest = Path(skill["root"]) / "mcp_servers.json"
            if manifests is not None:
                try:
                    stat = manifest.stat()
                    identity = (stat.st_mtime_ns, stat.st_size)
                except OSError:
                    identity = None
                manifests.append((str(manifest), identity))
            if not manifest.is_file():
                continue
            def report(reason, name=None):
                if issues is not None:
                    issues.append({"skill": str(skill.get("name", manifest.parent.name)), "path": str(manifest), "name": name, "error": reason})
            try:
                document = json.loads(manifest.read_text(encoding="utf-8"))
                servers = document.get("mcpServers") if isinstance(document, dict) else None
                if not isinstance(servers, dict):
                    raise ValueError()
            except (ValueError, OSError):
                report("Skill MCP manifest must contain a valid mcpServers object")
                continue
            for name, raw in servers.items():
                if name in defaults:
                    continue  # Earlier mounted skills retain discovery precedence.
                try:
                    validate_name(name)
                    # Validate shapes before substituting placeholders, so malformed
                    # optional extensions cannot interrupt unrelated services.
                    item = validate_server(raw)
                    def resolve(value):
                        return value.replace("${python}", sys.executable).replace("${skill}", str(manifest.parent))
                    if "command" in item:
                        item["command"] = resolve(item["command"])
                        item["args"] = [resolve(value) for value in item["args"]]
                    defaults[name] = validate_server(item)
                except ValueError as exc:
                    report(str(exc), name)
        return defaults

    def read(self, *, include_skills=False, issues=None):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"mcpServers": {}}
            if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
                raise ValueError()
            data = transform(data, self.path, mcp=True)
            data.setdefault("mcpServers", {})
            if not include_skills:
                return data
            defaults = self.skill_servers(issues=issues)
            data["mcpServers"] = {**data["mcpServers"], **{name: item for name, item in defaults.items() if name not in data["mcpServers"]}}
            return data
        except (ValueError, OSError):
            raise ValueError("mcp_servers.json is invalid; repair the file before saving")

    def write(self, data):
        from xuloom.core.storage import atomic_json
        if not isinstance(data,dict) or not isinstance(data.get("mcpServers",{}),dict):raise ValueError("Invalid MCP settings document")
        protected=transform(data, self.path, protect=True, mcp=True)
        for server in protected.get("mcpServers",{}).values():
            auth=server.get("auth",{}) if isinstance(server,dict) else {}
            if isinstance(auth,dict) and auth.get("client_secret") and not is_protected(auth["client_secret"]):
                raise ValueError("OAuth Client Secret requires native credential protection; install secrets support")
        atomic_json(self.path, protected)

    def snapshot(self, *, issues=None):
        servers = []
        for name, raw in self.read(include_skills=True, issues=issues)["mcpServers"].items():
            if not isinstance(raw, dict):
                servers.append({"name": name, "error": "Configuration must be an object", "enabled": False})
                continue
            config = copy.deepcopy(raw)
            error = ""
            try:
                validate_name(name)
                config = validate_server(raw)
            except ValueError as exc:
                error = str(exc)
                transport = raw.get("type", "http" if "url" in raw else "stdio")
                config = {"enabled": raw.get("enabled", True) is True, "type": transport if isinstance(transport, str) and transport in {"http", "stdio", "streamable-http"} else ""}
            # Both env and headers may contain credentials. Show names, never values.
            if unavailable_fields(config):
                error = "受保护凭据不可用；请重新填写此服务的凭据，原值已保留。"
            if config.get("auth",{}).get("type")=="oauth":
                from xuloom.protocols.oauth_store import OAuthStore
                config["oauth"]=OAuthStore(self,name,config).snapshot()
                if "client_secret" in config["auth"]:config["auth"]["client_secret"]=""
            for key in ("env", "headers"):
                values = config.get(key, {})
                config[key] = {k: "" for k in values} if isinstance(values, dict) else {}
            # Existing configurations may put a credential in URL query parameters.
            url = config.get("url", "")
            if is_protected(url):
                config["url"] = url.get("preview", "")
                config["savedUrlQuery"] = True
            if isinstance(url, str) and "?" in url:
                config["url"] = url.split("?", 1)[0]
                config["savedUrlQuery"] = True
            servers.append({**config, "name": name, "error": error})
        return servers

    @serialized_edit
    def save(self, name, config, *, existing=False):
        validate_name(name)
        if type(existing) is not bool or not isinstance(config, dict):
            raise ValueError("Provide an MCP configuration object and a boolean existing flag")
        for key in ("env", "headers"):
            if key in config and not isinstance(config[key], dict):
                raise ValueError(key + " must be a JSON object of strings")
        data = self.read()
        previous = data["mcpServers"].get(name)
        if previous is None:
            previous = self.read(include_skills=True)["mcpServers"].get(name)
        if previous is not None and not existing:
            raise ValueError("A server with this name already exists")
        if existing and previous is None:
            raise ValueError("MCP server no longer exists")
        config = copy.deepcopy(config)
        if isinstance(previous, dict) and existing:
            auth=config.get("auth")
            old=previous.get("auth",{})
            if isinstance(auth,dict) and auth.get("client_secret")=="" and isinstance(old,dict) and old.get("client_secret"):
                if any(auth.get(key)!=old.get(key) for key in ("client_id","issuer")):
                    raise ValueError("Client ID or issuer changed; re-enter or explicitly clear the previous Client Secret")
                auth["client_secret"]=old["client_secret"]
            for field in ("tool_timeout_sec","label","catalogId"):
                if field not in config and field in previous:config[field]=previous[field]
            for key in ("env", "headers"):
                old_values = previous.get(key, {})
                if not isinstance(old_values, dict):
                    old_values = {}
                for field, value in config.get(key, {}).items():
                    if value == "" and field in old_values:
                        config[key][field] = old_values[field]
            previous_url = previous.get("url", "")
            previous_preview = previous_url.get("preview", "") if is_protected(previous_url) else str(previous_url).split("?", 1)[0]
            if config.pop("savedUrlQuery", False) and config.get("url") == previous_preview:
                config["url"] = previous["url"]
        data["mcpServers"][name] = validate_server(config)
        self.write(data)
        if isinstance(previous,dict) and isinstance(previous.get("auth"),dict) and previous["auth"].get("type")=="oauth":
            from xuloom.protocols.oauth_store import OAuthStore
            try:comparison=validate_server(previous)
            except ValueError:comparison=previous
            if OAuthStore.fingerprint(comparison)!=OAuthStore.fingerprint(data["mcpServers"][name]):
                OAuthStore(self,name,data["mcpServers"][name]).forget_sync()

    @serialized_edit
    def migrate(self):
        if self.path.is_file():
            self.write(self.read())

    @serialized_edit
    def toggle(self, name, enabled):
        validate_name(name)
        if type(enabled) is not bool:
            raise ValueError("enabled must be a JSON boolean (true or false)")
        data = self.read()
        if name not in data["mcpServers"]:
            builtin = self.read(include_skills=True)["mcpServers"].get(name)
            if builtin is None:
                raise ValueError("Unknown MCP server")
            data["mcpServers"][name] = builtin
        row = data["mcpServers"][name]
        if not isinstance(row, dict):
            raise ValueError("Configuration must be an object; edit or remove this service")
        row["enabled"] = enabled
        if enabled:
            validate_server(row)
        self.write(data)

    @serialized_edit
    def remove(self, name):
        data = self.read()
        previous=data["mcpServers"].get(name)
        bundled = self.skill_servers()
        if name in bundled:
            data["mcpServers"][name] = {**bundled[name], "enabled": False}
        elif name not in data["mcpServers"]:
            raise ValueError("Unknown MCP server")
        else:
            del data["mcpServers"][name]
        self.write(data)
        if isinstance(previous,dict) and isinstance(previous.get("auth"),dict) and previous["auth"].get("type")=="oauth":
            from xuloom.protocols.oauth_store import OAuthStore
            OAuthStore(self,name,previous).forget_sync(check_config=False)
