"""Release archives must preserve binary wheel bytes on Windows."""
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from . import build_kit


class BuildKitTests(unittest.TestCase):
    def test_broken_source_or_overlay_wheel_cannot_replace_published_kit(self):
        for replacement in (False, True):
            with self.subTest(overlay=replacement), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root/'dist').mkdir()
                overlay = root/'fleet/runner-overlay/wheels'
                overlay.mkdir(parents=True)
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, 'w') as archive:
                    archive.writestr('package/data', b'binary\r\npayload')
                # Same failure shape as the old release: lost bytes before ZIP data.
                wheel = buffer.getvalue()[3:]
                source = root/'dist/ascend-bench-fleet-kit.tar.gz'
                with tarfile.open(source, 'w:gz') as archive:
                    if not replacement:
                        member = tarfile.TarInfo('ascend-bench-kit/wheels/broken.whl')
                        member.size = len(wheel)
                        archive.addfile(member, io.BytesIO(wheel))
                if replacement:
                    (overlay/'broken.whl').write_bytes(wheel)
                original = source.read_bytes()
                checksum = source.with_name(source.name+'.sha256')
                checksum.write_text('existing checksum')
                with patch.object(build_kit, 'ROOT', root):
                    with self.assertRaisesRegex(ValueError, 'Invalid release wheel.*broken.whl'):
                        build_kit.build()
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(checksum.read_text(), 'existing checksum')

    def test_binary_wheels_remain_valid_when_added_and_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'dist').mkdir()
            overlay = root/'fleet/runner-overlay'
            (overlay/'wheels').mkdir(parents=True)
            (overlay/'scripts').mkdir()
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as archive:
                archive.writestr('package/payload.bin', b'\x00binary\r\n\xff')
            wheel = buffer.getvalue()
            for name in ('existing.whl', 'new.whl'):
                (overlay/'wheels'/name).write_bytes(wheel)
            (overlay/'scripts/runner.py').write_bytes(b'print("ok")\r\n')
            source = root/'dist/ascend-bench-fleet-kit.tar.gz'
            with tarfile.open(source, 'w:gz') as archive:
                member = tarfile.TarInfo('ascend-bench-kit/wheels/existing.whl')
                member.size = len(wheel)
                archive.addfile(member, io.BytesIO(wheel))
            with patch.object(build_kit, 'ROOT', root):
                build_kit.build()
            with tarfile.open(source) as archive:
                for name in ('existing.whl', 'new.whl'):
                    raw = archive.extractfile('ascend-bench-kit/wheels/'+name).read()
                    self.assertEqual(raw, wheel)
                    with zipfile.ZipFile(io.BytesIO(raw)) as package:
                        self.assertIsNone(package.testzip())
                self.assertEqual(archive.extractfile('ascend-bench-kit/scripts/runner.py').read(), b'print("ok")\n')
            self.assertEqual(source.with_name(source.name+'.sha256').read_text().split()[0], hashlib.sha256(source.read_bytes()).hexdigest())
