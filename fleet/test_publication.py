import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from . import build_kit


class PublicationTests(unittest.TestCase):
    def test_runtime_payload_kept_development_files_removed_and_owner_erased(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'dist').mkdir()
            # Both the single-Runner and fleet builders consume their own kit.
            archive_path = root/'dist/ascend-bench-fleet-kit.tar.gz'
            payloads = {'benchmark/ais_bench/core.py': b'VALUE = 7\n',
                        'benchmark/tests/test_old.py': b'development fixture',
                        'benchmark/smoke_tests/case.yaml': b'development fixture',
                        'benchmark/plugins/example/tests/test_old.py': b'development fixture',
                        'scripts/__pycache__/old.pyc': b'intermediate',
                        'benchmark/LICENSE': b'upstream license'}
            with tarfile.open(archive_path, 'w:gz') as archive:
                for name, raw in payloads.items():
                    member = tarfile.TarInfo('ascend-bench-kit/'+name)
                    member.size = len(raw)
                    member.uid = member.gid = 1234
                    member.uname = member.gname = 'private-builder'
                    member.pax_headers = {'comment': 'private-build-host'}
                    archive.addfile(member, io.BytesIO(raw))
            with patch.object(build_kit, 'ROOT', root):
                build_kit.build()
            with tarfile.open(archive_path) as archive:
                names = archive.getnames()
                self.assertEqual(archive.extractfile('ascend-bench-kit/benchmark/ais_bench/core.py').read(), payloads['benchmark/ais_bench/core.py'])
                self.assertIn('ascend-bench-kit/benchmark/LICENSE', names)
                for name in list(payloads)[1:5]:
                    self.assertNotIn('ascend-bench-kit/'+name, names)
                for member in archive:
                    self.assertEqual((member.uid, member.gid, member.uname, member.gname), (0, 0, '', ''))
                    self.assertNotIn('comment', member.pax_headers)


if __name__ == '__main__':
    unittest.main()
