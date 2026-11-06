import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest


class ProvenanceTests(unittest.TestCase):
    def test_release_rejects_dirty_stale_or_tampered_validation(self):
        verify=runpy.run_path(str(Path(__file__).resolve().parents[1]/'desktop/scripts/release.py'))['verify_provenance']
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);sources={'xuloom/fixture.py':'hash'}
            receipt={'passed':True,'cleanSource':True,'sourceCommit':'commit-fixture','sources':sources}
            payload=json.dumps(receipt).encode();(root/'build-validation.json').write_bytes(payload)
            provenance={'sourceCommit':'commit-fixture','dirty':False,'sourceHashes':sources,'validationReceiptSha256':hashlib.sha256(payload).hexdigest()}
            file=root/'build-provenance.json';file.write_text(json.dumps(provenance))
            self.assertEqual(verify(root,'commit-fixture',sources),provenance)
            for changed in ({**provenance,'dirty':True},{**provenance,'sourceCommit':'other-commit'},{**provenance,'validationReceiptSha256':'wrong'}):
                file.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):verify(root,'commit-fixture',sources)
            file.write_text(json.dumps(provenance));(root/'build-validation.json').write_bytes(payload+b' ')
            with self.assertRaises(ValueError):verify(root,'commit-fixture',sources)
