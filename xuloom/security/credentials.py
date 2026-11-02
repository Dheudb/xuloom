"""OS-protected configuration fields, stored with the existing atomic document.

Windows uses current-user DPAPI. Optional macOS/Linux backends keep an encryption
key in the native credential service. Protected values that cannot be opened
remain opaque so settings can repair them; execution must call require_secret
before using a credential.
"""
import base64
import binascii
import ctypes
import hashlib
import json
import os
from pathlib import Path

MARKER = "$xuloom_secret"
BACKEND = "dpapi-v1"
KEYRING_BACKEND = "system-keyring-v1"
SECRET_NAMES = {"api_key", "apikey", "password", "client_secret", "access_token", "refresh_token", "oauth_state"}


class CredentialUnavailable(ValueError):
    def __init__(self):
        super().__init__("受保护凭据不可用；请在原用户及配置位置使用，或在设置中重新填写。原值已保留，未使用其他 Key。")


def is_protected(value):
    return isinstance(value, dict) and MARKER in value


def require_secret(value):
    if is_protected(value):
        raise CredentialUnavailable()
    if not isinstance(value, str):
        raise ValueError("Credential must be a string")
    return value


def api_key(value):
    value = require_secret(value)
    if len(value) > 8192 or not value.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("API Key 格式无效；请填写不含控制字符的 ASCII 凭据。")
    return value


def protection_mode():
    preference=os.environ.get("XULOOM_CREDENTIAL_STORAGE","auto")
    if preference not in {"auto","required","plaintext"}:
        raise ValueError("XULOOM_CREDENTIAL_STORAGE must be auto, required or plaintext")
    if preference=="plaintext": return "file-only"
    if os.name=="nt": return "windows-dpapi"
    from xuloom.security.system_keyring import mode
    selected=mode()
    return selected or ("unavailable" if preference=="required" else "file-only")


def native(data, entropy, *, decrypt=False):
    if os.name != "nt":
        raise CredentialUnavailable()
    from ctypes import wintypes as W
    class Blob(ctypes.Structure):
        _fields_ = [("size", W.DWORD), ("data", ctypes.c_void_p)]
    source = ctypes.create_string_buffer(data)
    context = ctypes.create_string_buffer(entropy)
    source_blob = Blob(len(data), ctypes.cast(source, ctypes.c_void_p))
    context_blob = Blob(len(entropy), ctypes.cast(context, ctypes.c_void_p))
    output = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                         ctypes.c_void_p, W.DWORD, ctypes.POINTER(Blob)]
    function.restype = W.BOOL
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        if not function(ctypes.byref(source_blob), None, ctypes.byref(context_blob), None, None, 1, ctypes.byref(output)):
            raise CredentialUnavailable()
        return ctypes.string_at(output.data, output.size)
    finally:
        if output.data:
            ctypes.memset(output.data, 0, output.size)
            kernel.LocalFree(output.data)
        ctypes.memset(source, 0, len(data))


def transform(document, filename, *, protect=False, mcp=False):
    """Return a copy; native protection may initialize its OS-held key on save."""
    scope = os.path.normcase(str(Path(filename).resolve()))
    keyring_context = None
    keyring_failed = False
    def keyring_crypt(data, entropy, *, decrypt=False):
        # One OS lookup per document, never a process-wide unlocked-key cache.
        nonlocal keyring_context, keyring_failed
        from xuloom.security.system_keyring import cipher, crypt
        if keyring_failed: raise CredentialUnavailable()
        if keyring_context is None:
            try: keyring_context=cipher(create=not decrypt)
            except CredentialUnavailable:
                keyring_failed=True
                raise
        return crypt(data,entropy,decrypt=decrypt,context=keyring_context)
    def secret(value, location, *, url=False):
        entropy = hashlib.sha256(json.dumps(["xuloom.credentials.v1", scope, location], ensure_ascii=False).encode()).digest()
        if is_protected(value):
            if protect:
                return dict(value)  # Preserve unavailable values during metadata edits.
            try:
                if (value.get(MARKER) not in {BACKEND,KEYRING_BACKEND} or value.keys() - {MARKER, "data", "preview"} or
                        not isinstance(value.get("data"), str) or len(value["data"]) > 2_000_000):
                    raise CredentialUnavailable()
                if value[MARKER]==KEYRING_BACKEND:
                    crypt=keyring_crypt
                else:
                    crypt=native
                return crypt(base64.b64decode(value["data"], validate=True), entropy, decrypt=True).decode("utf-8")
            except (ValueError, OSError, UnicodeError, binascii.Error):
                return dict(value)
        if not protect or not value:
            return value
        require_secret(value)
        mode=protection_mode()
        if mode=="file-only": return value
        if mode=="unavailable":
            raise CredentialUnavailable()
        content = value.encode("utf-8")
        if len(content) > 1_000_000:
            raise ValueError("Credential exceeds the supported size")
        if mode=="windows-dpapi":
            backend,crypt=BACKEND,native
        else:
            backend,crypt=KEYRING_BACKEND,keyring_crypt
        result = {MARKER: backend, "data": base64.b64encode(crypt(content, entropy)).decode("ascii")}
        if url:
            result["preview"] = value.split("?", 1)[0]
        return result
    def visit(value, location):
        if isinstance(value, list):
            return [visit(item, location + [index]) for index, item in enumerate(value)]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Configuration field names must be strings")
            name = key.lower()
            where = location + [key]
            server = mcp and len(location) == 2 and location[0] == "mcpServers"
            credential = not mcp and (not location and (name in SECRET_NAMES or name.endswith("_api_key")) or
                len(location) == 2 and location[0] in {"model_connections", "media_connections"} and name == "api_key")
            if server and name in {"env", "headers"} and isinstance(item, dict):
                result[key] = {field: secret(content, where + [field]) for field, content in item.items()}
            elif server and name=="auth" and isinstance(item,dict):
                result[key]={field:secret(content,where+[field]) if field=="client_secret" else visit(content,where+[field]) for field,content in item.items()}
            elif credential:
                result[key] = secret(item, where)
            elif server and name == "url" and (is_protected(item) or isinstance(item, str) and "?" in item):
                result[key] = secret(item, where, url=True)
            else:
                result[key] = visit(item, where)
        return result
    return visit(document, [])


def unavailable_fields(value, prefix=""):
    if is_protected(value):
        return [prefix]
    items = enumerate(value) if isinstance(value, list) else value.items() if isinstance(value, dict) else []
    return [path for key, item in items for path in unavailable_fields(item, prefix + "/" + str(key))]


def storage_status(config=None):
    return {"mode": protection_mode(),
            "unavailable": unavailable_fields(config or {})}
