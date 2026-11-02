"""Real AEAD/OS leases with owned native-store substitutes on any host.

These tests do not impersonate macOS Keychain or a real Secret Service daemon.
Native hosts have separate opt-in integration gates below.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from xuloom.core import config
from xuloom.security import credentials, system_keyring


class OwnedStore:
    def __init__(self):
        self.values={}; self.created=0; self.mutex=threading.Lock(); self.failure=False

    def get(self,account):
        if self.failure: raise OSError("owned store locked")
        with self.mutex: value=self.values.get(account)
        if value is None: time.sleep(.025)
        return value

    def create(self,account,value):
        if self.failure: raise OSError("owned store locked")
        with self.mutex:
            if account in self.values: raise OSError("duplicate creation")
            self.values[account]=value;self.created+=1


class SystemKeyringTests(unittest.TestCase):
    def setUp(self):
        try: from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        except ImportError: self.skipTest("Optional cryptography dependency")
        temp=tempfile.TemporaryDirectory(prefix="xuloom-system-keyring-")
        self.addCleanup(temp.cleanup);self.root=Path(temp.name);self.path=self.root/"config.json"
        self.backend=OwnedStore()
        for target,value in [("xuloom.core.config.CONFIG_FILE",self.path),
            ("xuloom.security.credentials.protection_mode",lambda:"secret-service"),
            ("xuloom.security.system_keyring.store",lambda:self.backend)]:
            fixture=patch(target,value);fixture.start();self.addCleanup(fixture.stop)

    def test_long_fields_use_one_system_key_and_aead_is_context_bound(self):
        value={"api_key":"owned-key-"*2048,"refresh_token":"轮换凭据"*1024,"regular":"keep"}
        config.save_config(value)
        self.assertEqual(config.load_config(),value)
        self.assertEqual(self.backend.created,1)
        raw=config.load_config(decrypt_secrets=False)
        self.assertEqual(raw["api_key"][credentials.MARKER],credentials.KEYRING_BACKEND)
        text=self.path.read_text()
        self.assertNotIn(value["api_key"],text)
        self.assertNotIn(next(iter(self.backend.values.values())),text,"Master key never enters the configuration")
        bad=json.loads(text);data=bytearray(base64.b64decode(bad["api_key"]["data"]));data[-1]^=1
        bad["api_key"]["data"]=base64.b64encode(data).decode()
        self.assertTrue(credentials.is_protected(credentials.transform(bad,self.path)["api_key"]))
        self.assertTrue(credentials.is_protected(credentials.transform(raw,self.root/"copy.json")["api_key"]))

    def test_document_uses_one_native_lookup_and_corrupt_field_does_not_hide_healthy_fields(self):
        config.save_config({"api_key":"one","refresh_token":"two"})
        raw=config.load_config(decrypt_secrets=False)
        data=bytearray(base64.b64decode(raw["api_key"]["data"]));data[-1]^=1
        raw["api_key"]["data"]=base64.b64encode(data).decode()
        with patch.object(self.backend,"get",wraps=self.backend.get) as get:
            result=credentials.transform(raw,self.path)
            self.assertEqual(get.call_count,1)
            self.assertTrue(credentials.is_protected(result["api_key"]))
            self.assertEqual(result["refresh_token"],"two")
        self.backend.failure=True
        with patch.object(self.backend,"get",wraps=self.backend.get) as get:
            result=credentials.transform(raw,self.path)
            self.assertEqual(get.call_count,1)
            self.assertTrue(credentials.is_protected(result["refresh_token"]))

    def test_parallel_initializers_cannot_replace_each_others_key(self):
        entropy=b"owned-entropy"
        with ThreadPoolExecutor(max_workers=6) as executor:
            results=list(executor.map(lambda i:system_keyring.crypt(str(i).encode(),entropy),range(6)))
        self.assertEqual(self.backend.created,1)
        self.assertEqual([system_keyring.crypt(value,entropy,decrypt=True) for value in results],[str(i).encode() for i in range(6)])
        self.assertEqual(len(set(results)),6)

    def test_missing_key_read_does_not_create_a_key_directory_or_lock(self):
        missing=self.root/"not-created"/"config.json"
        with patch.object(config,"CONFIG_FILE",missing):
            with self.assertRaises(credentials.CredentialUnavailable): system_keyring.crypt(b"x"*40,b"context",decrypt=True)
        self.assertFalse(missing.parent.exists())
        self.assertEqual(self.backend.created,0)

    def test_locked_or_corrupt_key_never_downgrades_a_failed_save(self):
        config.save_config({"api_key":"original-owned-key"})
        original=self.path.read_bytes()
        self.backend.failure=True
        self.assertTrue(credentials.is_protected(config.load_config()["api_key"]))
        with self.assertRaises(credentials.CredentialUnavailable): config.save_config({"api_key":"replacement-owned-key"})
        self.assertEqual(self.path.read_bytes(),original)
        self.backend.failure=False
        account=next(iter(self.backend.values));self.backend.values[account]="corrupt-key"
        with self.assertRaises(credentials.CredentialUnavailable): config.save_config({"api_key":"replacement-owned-key"})
        self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(self.backend.created,1)

    def test_failed_publish_keeps_previous_secrets_and_reuses_the_native_key(self):
        config.save_config({"api_key":"original-owned-key"})
        original=self.path.read_bytes()
        with patch("xuloom.core.storage.os.replace",side_effect=OSError("owned failed publish")):
            with self.assertRaises(OSError): config.save_config({"api_key":"new-owned-key"})
        self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(config.load_config()["api_key"],"original-owned-key")
        self.assertEqual(self.backend.created,1)
        self.assertFalse(list(self.root.glob(".migration-*")))

    def test_required_mode_refuses_missing_backend_and_plaintext_is_explicit(self):
        # Test the actual selector, replacing only the platform-native detector.
        # setUp replaced the global attribute, so use its original reference.
        selector=self.selector
        with patch("xuloom.security.credentials.os.name","posix"), patch("xuloom.security.system_keyring.mode",return_value=None):
            with patch.dict(os.environ,{"XULOOM_CREDENTIAL_STORAGE":"required"}):
                self.assertEqual(selector(),"unavailable")
            with patch.dict(os.environ,{"XULOOM_CREDENTIAL_STORAGE":"plaintext"}):
                self.assertEqual(selector(),"file-only")
        with patch("xuloom.security.credentials.protection_mode",return_value="unavailable"):
            with self.assertRaises(credentials.CredentialUnavailable): config.save_config({"api_key":"owned"})
        self.assertFalse(self.path.exists())

    def test_explicit_plaintext_does_not_disable_reading_an_existing_native_value(self):
        config.save_config({"api_key":"owned-original"})
        with patch("xuloom.security.credentials.protection_mode",return_value="file-only"):
            self.assertEqual(config.load_config()["api_key"],"owned-original")
            config.save_config({"api_key":"owned-explicit-plaintext"})
        self.assertEqual(json.loads(self.path.read_text())["api_key"],"owned-explicit-plaintext")

    selector=staticmethod(credentials.protection_mode)


class NativeStoreContractTests(unittest.TestCase):
    def test_native_reply_deadline_and_no_interactive_prompt(self):
        from types import SimpleNamespace
        from enum import Enum
        from unittest.mock import Mock
        class Field(Enum): member=1
        raw=Mock();connection=system_keyring.BoundedConnection(raw)
        message=SimpleNamespace(header=SimpleNamespace(fields={Field.member:"Get"}))
        connection.send_and_get_reply(message)
        self.assertGreater(raw.send_and_get_reply.call_args.kwargs["timeout"],0)
        self.assertLessEqual(raw.send_and_get_reply.call_args.kwargs["timeout"],5)
        message.header.fields[Field.member]="Prompt"
        with self.assertRaises(OSError): connection.send_and_get_reply(message)
        self.assertEqual(raw.send_and_get_reply.call_count,1)
        connection.deadline=time.monotonic()-1
        with self.assertRaises(TimeoutError): connection.recv_until_filtered([])
        raw.recv_until_filtered.assert_not_called()

    def test_secret_service_does_not_unlock_or_create_collections_on_read(self):
        from types import SimpleNamespace
        class Connection:
            def close(self): pass
        class Collection:
            def is_locked(self): return True
            def unlock(self): raise AssertionError("Must not unlock")
        fake=SimpleNamespace(dbus_init=Connection,Collection=lambda connection:Collection())
        with patch.dict("sys.modules",{"secretstorage":fake}):
            with self.assertRaises(OSError): system_keyring.SecretService().get("owned")

    def test_ambiguous_secret_service_matches_are_refused(self):
        from types import SimpleNamespace
        class Connection:
            def close(self): pass
        class Collection:
            def is_locked(self): return False
            def search_items(self,attributes): return [object(),object()]
        fake=SimpleNamespace(dbus_init=Connection,Collection=lambda connection:Collection())
        with patch.dict("sys.modules",{"secretstorage":fake}):
            with self.assertRaises(OSError): system_keyring.SecretService().get("owned")


@unittest.skipUnless(sys.platform in {"darwin","linux"} and os.environ.get("XULOOM_TEST_NATIVE_KEYRING")=="1",
                     "Opt-in real macOS/Linux native credential service")
class NativeStoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(system_keyring.mode(),"Install secrets dependencies before the native gate")
        temp=tempfile.TemporaryDirectory(prefix="xuloom-native-keyring-gate-")
        self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        fixture=patch.object(config,"CONFIG_FILE",self.root/"config.json");fixture.start();self.addCleanup(fixture.stop)
        environment=patch.dict(os.environ,{"XULOOM_CREDENTIAL_STORAGE":"required"});environment.start();self.addCleanup(environment.stop)
        account=hashlib.sha256(os.path.normcase(str(self.root.resolve())).encode()).hexdigest()
        backend=system_keyring.store()
        self.addCleanup(backend.delete,account)  # Only this test-created namespace.

    def test_real_native_roundtrip_and_field_tampering(self):
        config.save_config({"api_key":"owned-native-gate-key"})
        self.assertEqual(config.load_config()["api_key"],"owned-native-gate-key")
        self.assertNotIn("owned-native-gate-key",config.CONFIG_FILE.read_text())
        raw=config.load_config(decrypt_secrets=False)
        self.assertTrue(credentials.is_protected(credentials.transform(raw,self.root/"other.json")["api_key"]))

    def test_real_fresh_process_can_open_the_test_owned_native_value(self):
        config.save_config({"api_key":"owned-native-gate-key"})
        env={**os.environ,"XULOOM_CONFIG_DIR":str(self.root),"HOME":str(self.root/"home"),"XULOOM_SKILLS_PATHS":""}
        result=subprocess.run([sys.executable,"-c","from xuloom.core.config import load_config;assert load_config()['api_key']=='owned-native-gate-key';print('NATIVE_CHILD_OK')"],
            env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),"NATIVE_CHILD_OK")
