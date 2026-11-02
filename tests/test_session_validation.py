"""Invalid checkpoints must not become write baselines or filesystem addresses."""
import json
from pathlib import Path
import tempfile
import unittest

from xuloom.core.session import SessionManager
from xuloom.core.session_lock import SessionConflictError, SessionLock
from xuloom.core.storage import atomic_json


class SessionValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='xuloom-session-validation-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = SessionManager(session_dir=self.root / 'sessions', cwd=self.root)
        self.addCleanup(self.manager.release_writer)
        self.path = self.root / 'sessions/main.json'
        self.child = self.root / 'sessions/subagents/child.json'
        self.payload = {'messages': [{'role': 'user', 'content': 'saved'}], 'metadata': {}, 'revision': 3}

    def test_invalid_revisions_never_prime_main_or_child_versions(self):
        for value in (None, True, False, -1, 1.0, '3', {}, []):
            for path, load in ((self.path, lambda: self.manager.load_session_state('main')),
                               (self.child, lambda: self.manager.load_subagent_transcript('child'))):
                with self.subTest(value=value, path=path.name):
                    atomic_json(path, {**self.payload, 'revision': value})
                    before = path.read_bytes()
                    with self.assertRaises(SessionConflictError): load()
                    self.assertNotIn(str(path), self.manager._versions)
                    self.assertEqual(path.read_bytes(), before)

    def test_existing_good_version_survives_a_rejected_reload(self):
        atomic_json(self.path, self.payload)
        self.manager.load_session_state('main')
        before = dict(self.manager._versions)
        atomic_json(self.path, {**self.payload, 'revision': None})
        with self.assertRaises(SessionConflictError): self.manager.load_session_state('main')
        self.assertEqual(self.manager._versions, before)
        with self.assertRaises(SessionConflictError): self.manager.check_current('main')
        corrupt = self.path.read_bytes()
        self.assertFalse(self.manager.save_session_state('main', []))
        self.assertEqual(self.path.read_bytes(), corrupt)

    def test_missing_revision_and_legacy_list_upgrade_once(self):
        for raw in ({'messages': [], 'metadata': {}}, []):
            atomic_json(self.path, raw)
            self.assertTrue(self.manager.load_session_state('main')['_valid'])
            self.assertTrue(self.manager.save_session_state('main', [{'role': 'user', 'content': 'next'}]))
            self.assertEqual(json.loads(self.path.read_bytes())['revision'], 1)
        self.assertEqual(self.manager.load_session_state('missing'), {'messages': [], 'metadata': {}})

    def test_invalid_shapes_and_bad_json_are_preserved(self):
        invalid = [None, 3, 'text', {'messages': None}, {'messages': 'text'}, {'metadata': []}, {'metadata': None}]
        for value in invalid:
            atomic_json(self.path, value)
            before = self.path.read_bytes()
            with self.subTest(value=value), self.assertRaises(SessionConflictError):
                self.manager.load_session_state('main')
            self.assertEqual(self.path.read_bytes(), before)
        self.path.write_bytes(b'{"revision":')
        with self.assertRaises(SessionConflictError): self.manager.load_session_state('main')
        atomic_json(self.child, [])
        with self.assertRaises(SessionConflictError): self.manager.load_subagent_transcript('child')
        self.assertEqual(self.manager._versions, {})

    def test_save_rejects_invalid_new_messages_without_creating_a_checkpoint(self):
        self.assertFalse(self.manager.save_session_state('main', 'not messages'))
        self.assertFalse(self.path.exists())
        self.assertFalse(self.manager.owns_writer('main'))
        self.assertFalse(self.manager.save_subagent_transcript('child', {'session_id': 'main', 'messages': None}))
        self.assertFalse(self.child.exists())

    def test_ids_cannot_escape_the_session_directory(self):
        outside = self.root / 'outside.json'; atomic_json(outside, self.payload)
        before = outside.read_bytes()
        for identity in ('../outside', '../../outside', '..\\outside', '/absolute', 'C:\\outside', '', 'line\nbreak',
                         'x' * 161, None, True, [], {}):
            with self.subTest(identity=identity):
                with self.assertRaises(ValueError): self.manager.load_session_state(identity)
                with self.assertRaises(ValueError): self.manager.load_subagent_transcript(identity)
                with self.assertRaises(ValueError): self.manager.get_subagent_transcript_path(identity)
                with self.assertRaises(ValueError): self.manager.check_current(identity)
                with self.assertRaises(ValueError): self.manager.delete_session(identity)
                self.assertFalse(self.manager.save_session_state(identity, []))
                self.assertFalse(self.manager.save_subagent_transcript(identity, {'session_id': 'main', 'messages': []}))
                self.assertEqual(outside.read_bytes(), before)

    def test_valid_legacy_identity_limit_still_matches_writer_lease(self):
        identity = 'a' * 160
        self.assertTrue(self.manager.save_session_state(identity, []))
        self.assertTrue(self.manager.load_session_state(identity)['_valid'])
        self.assertTrue(SessionLock(self.root / 'locks', identity))


if __name__ == '__main__': unittest.main()
