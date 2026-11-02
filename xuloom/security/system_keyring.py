"""Optional POSIX native key storage for one per-configuration encryption key.

Only the OS Keychain/Secret Service is used; no plugin selection, file key store,
automatic unlock dialog, or plaintext fallback on a native operation failure.
"""
import base64
from contextlib import ExitStack, closing
import ctypes as C
import hashlib
import importlib.util
import os
import sys
import time


SERVICE = "org.xuloom.credentials.v1"


def mode():
    if importlib.util.find_spec("cryptography") is None:
        return None
    if sys.platform == "darwin":
        return "macos-keychain"
    if sys.platform.startswith("linux") and importlib.util.find_spec("secretstorage") is not None:
        return "secret-service"
    return None


class MacKeychain:
    def __init__(self):
        from ctypes.util import find_library
        self.security = C.CDLL(find_library("Security"))
        self.core = C.CDLL(find_library("CoreFoundation"))
        for library, name, args, result in [
            (self.core,"CFDictionaryCreateMutable",[C.c_void_p,C.c_long,C.c_void_p,C.c_void_p],C.c_void_p),
            (self.core,"CFDictionarySetValue",[C.c_void_p,C.c_void_p,C.c_void_p],None),
            (self.core,"CFStringCreateWithCString",[C.c_void_p,C.c_char_p,C.c_uint32],C.c_void_p),
            (self.core,"CFDataCreate",[C.c_void_p,C.c_void_p,C.c_long],C.c_void_p),
            (self.core,"CFDataGetLength",[C.c_void_p],C.c_long),
            (self.core,"CFDataGetBytePtr",[C.c_void_p],C.c_void_p),
            (self.core,"CFRelease",[C.c_void_p],None),
            (self.security,"SecItemCopyMatching",[C.c_void_p,C.POINTER(C.c_void_p)],C.c_int32),
            (self.security,"SecItemAdd",[C.c_void_p,C.c_void_p],C.c_int32),
            (self.security,"SecItemDelete",[C.c_void_p],C.c_int32),
        ]:
            function=getattr(library,name);function.argtypes=args;function.restype=result

    def constant(self, name):
        return C.c_void_p.in_dll(self.security,name).value

    def query(self, resources, account):
        keys=C.addressof(C.c_char.in_dll(self.core,"kCFTypeDictionaryKeyCallBacks"))
        values=C.addressof(C.c_char.in_dll(self.core,"kCFTypeDictionaryValueCallBacks"))
        query=self.core.CFDictionaryCreateMutable(None,0,keys,values)
        if not query: raise OSError("Cannot allocate a Keychain query")
        resources.callback(self.core.CFRelease,query)
        self.core.CFDictionarySetValue(query,self.constant("kSecClass"),self.constant("kSecClassGenericPassword"))
        self.core.CFDictionarySetValue(query,self.constant("kSecUseAuthenticationUI"),self.constant("kSecUseAuthenticationUIFail"))
        for name,value in (("kSecAttrService",SERVICE),("kSecAttrAccount",account)):
            string=self.core.CFStringCreateWithCString(None,value.encode("utf-8"),0x08000100)
            if not string: raise OSError("Cannot allocate a Keychain attribute")
            resources.callback(self.core.CFRelease,string)
            self.core.CFDictionarySetValue(query,self.constant(name),string)
        return query

    def get(self, account):
        with ExitStack() as resources:
            query=self.query(resources,account)
            self.core.CFDictionarySetValue(query,self.constant("kSecReturnData"),C.c_void_p.in_dll(self.core,"kCFBooleanTrue").value)
            self.core.CFDictionarySetValue(query,self.constant("kSecMatchLimit"),self.constant("kSecMatchLimitOne"))
            output=C.c_void_p()
            status=self.security.SecItemCopyMatching(query,C.byref(output))
            if status == -25300: return None
            if status: raise OSError("Keychain read failed ("+str(status)+")")
            if not output.value: raise OSError("Keychain did not return data")
            resources.callback(self.core.CFRelease,output)
            size=self.core.CFDataGetLength(output)
            if not 0 < size <= 256: raise ValueError("Invalid native encryption key size")
            return C.string_at(self.core.CFDataGetBytePtr(output),size).decode("ascii")

    def create(self, account, value):
        with ExitStack() as resources:
            query=self.query(resources,account)
            encoded=value.encode("ascii")
            data=self.core.CFDataCreate(None,encoded,len(encoded))
            if not data: raise OSError("Cannot allocate Keychain data")
            resources.callback(self.core.CFRelease,data)
            self.core.CFDictionarySetValue(query,self.constant("kSecValueData"),data)
            status=self.security.SecItemAdd(query,None)
            if status: raise OSError("Keychain creation failed ("+str(status)+")")

    def delete(self, account):
        with ExitStack() as resources:
            status=self.security.SecItemDelete(self.query(resources,account))
            if status not in {0,-25300}: raise OSError("Keychain removal failed ("+str(status)+")")


class BoundedConnection:
    """Bound native replies and refuse interactive prompts in settings I/O."""
    def __init__(self, connection, timeout=5):
        self.connection=connection
        self.deadline=time.monotonic()+timeout

    def remaining(self, timeout=None):
        remaining=self.deadline-time.monotonic()
        if remaining<=0: raise TimeoutError("Native credential service did not reply")
        return min(remaining,timeout) if timeout is not None else remaining

    def send_and_get_reply(self, message, *, timeout=None):
        fields=message.header.fields
        member=next((value for field,value in fields.items() if getattr(field,"name",None)=="member"),None)
        if member in {"Prompt","Unlock"}:
            raise OSError("Unlock or authorize the credential service in the operating system")
        return self.connection.send_and_get_reply(message,timeout=self.remaining(timeout))

    def recv_until_filtered(self, queue, *, timeout=None):
        return self.connection.recv_until_filtered(queue,timeout=self.remaining(timeout))

    def __getattr__(self, name):
        return getattr(self.connection,name)


class SecretService:
    def collection(self, resources):
        import secretstorage
        connection=resources.enter_context(closing(secretstorage.dbus_init()))
        collection=secretstorage.Collection(BoundedConnection(connection))
        if collection.is_locked():
            raise OSError("Secret Service collection is locked; unlock it in the operating system")
        return collection

    def get(self, account):
        with ExitStack() as resources:
            collection=self.collection(resources)
            items=list(collection.search_items({"application":SERVICE,"account":account}))
            if not items: return None
            if len(items)!=1 or items[0].is_locked():
                raise OSError("Native encryption key is ambiguous or locked")
            value=items[0].get_secret()
            if not 0 < len(value) <= 256: raise ValueError("Invalid native encryption key size")
            return value.decode("ascii")

    def create(self, account, value):
        with ExitStack() as resources:
            self.collection(resources).create_item("Xuloom configuration encryption key",
                {"application":SERVICE,"account":account},value.encode("ascii"),replace=False)

    def delete(self, account):
        with ExitStack() as resources:
            collection=self.collection(resources)
            items=list(collection.search_items({"application":SERVICE,"account":account}))
            if len(items)>1: raise OSError("Native encryption key is ambiguous")
            for item in items:
                if item.is_locked(): raise OSError("Native encryption key is locked")
                item.delete()


def store():
    selected=mode()
    if selected=="macos-keychain": return MacKeychain()
    if selected=="secret-service": return SecretService()
    raise OSError("Install xuloom[secrets] and provide an unlocked native credential service")


def key(*, create):
    from xuloom.core.config import CONFIG_FILE
    from xuloom.core.session_lock import SessionBusyError, SessionLock
    root=CONFIG_FILE.resolve().parent
    account=hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()
    backend=store()
    def decode(value):
        if value is None: raise OSError("Native encryption key is missing")
        material=base64.b64decode(value,validate=True)
        if len(material)!=32: raise ValueError("Invalid native encryption key")
        return material
    value=backend.get(account)
    if value is not None or not create:
        return decode(value)  # Read-only queries never create locks or keys.
    lease=SessionLock(root,"credential-key-"+account)
    deadline=time.monotonic()+10
    try:
        while True:
            try:
                lease.acquire();break
            except SessionBusyError:
                if time.monotonic()>=deadline: raise TimeoutError("Native credential initialization is busy") from None
                time.sleep(.02)
        value=backend.get(account)
        if value is None:
            if not create: raise OSError("Native encryption key is missing")
            generated=os.urandom(32)
            value=base64.b64encode(generated).decode("ascii")
            backend.create(account,value)
            # A successful native write must be readable before publishing data.
            if backend.get(account)!=value: raise OSError("Native encryption key could not be verified")
        return decode(value)
    finally:
        lease.close()


def cipher(*, create):
    from xuloom.security.credentials import CredentialUnavailable
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key(create=create))
    except Exception:
        raise CredentialUnavailable() from None


def crypt(data, entropy, *, decrypt=False, context=None):
    from xuloom.security.credentials import CredentialUnavailable
    try:
        context=context if context is not None else cipher(create=not decrypt)
        if decrypt:
            if len(data)<28: raise ValueError("Invalid protected credential")
            return context.decrypt(data[:12],data[12:],entropy)
        nonce=os.urandom(12)
        return nonce+context.encrypt(nonce,data,entropy)
    except Exception:
        raise CredentialUnavailable() from None
