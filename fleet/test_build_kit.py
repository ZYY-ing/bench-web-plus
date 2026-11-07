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
