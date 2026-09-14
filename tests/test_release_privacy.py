import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NOTICE = 'OpenThesis/bin/openthesis-sidecar/_internal/numpy-2.4.6.dist-info/licenses/numpy/random/src/pcg64/LICENSE.md'


@unittest.skipUnless(os.name == 'nt', 'Windows packaging verification')
class ReleasePrivacyTests(unittest.TestCase):
    def verify(self, name, content):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / 'candidate.zip'
            with zipfile.ZipFile(archive, 'w') as target:
                target.writestr(name, content)
            return subprocess.run([shutil.which('pwsh') or 'powershell.exe', '-NoProfile', '-File',
                                   str(ROOT / 'scripts/verify-release-privacy.ps1'), '-Archive', str(archive)],
                                  capture_output=True, timeout=30)

    def test_exact_public_notice_is_retained(self):
        result = self.verify(NOTICE, (ROOT/'tests/fixtures/licenses/pcg64-LICENSE.md').read_bytes())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'PreservedReviewedPublicLicenseNotices', result.stdout)

    def test_license_path_cannot_hide_added_private_material(self):
        result = self.verify(NOTICE, (ROOT/'tests/fixtures/licenses/pcg64-LICENSE.md').read_bytes() + b'\nprivate-person@private.test\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'PersonalEmail', result.stderr)

    def test_known_notice_in_unreviewed_path_is_not_exempt(self):
        result = self.verify('OpenThesis/private.txt', (ROOT/'tests/fixtures/licenses/pcg64-LICENSE.md').read_bytes())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'PersonalEmail', result.stderr)

    def test_credentials_and_user_data_remain_forbidden(self):
        result = self.verify('OpenThesis/settings.json', b'-----BEGIN RSA PRIVATE KEY-----')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'PrivateKey', result.stderr)
        self.assertIn(b'forbidden data entry', result.stderr)
