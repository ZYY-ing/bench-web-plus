import hashlib
import io
import json
from pathlib import Path
import ssl
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from .pip_trust import REMOTE, system_ca_bundle, trust_payload


class PipTrustTests(unittest.TestCase):
    def test_exports_real_os_roots_without_disabling_verification(self):
        value=trust_payload({})
        self.assertEqual(hashlib.sha256(value['pem'].encode()).hexdigest(),value['sha256'])
        context=ssl.create_default_context(cadata=value['pem'])
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertNotIn('PRIVATE KEY',value['pem'])

    def test_explicit_admin_certificate_takes_precedence(self):
        with patch('fleet.pip_trust.system_ca_bundle') as export:
            self.assertIsNone(trust_payload({'pip_cert':'/mnt/certs/company.pem'}))
            export.assert_not_called()

    def test_empty_os_store_is_not_a_reason_to_disable_tls(self):
        with patch('fleet.pip_trust.ssl.create_default_context',return_value=types.SimpleNamespace(get_ca_certs=lambda **kw:[])):
            with self.assertRaisesRegex(ValueError,'不会关闭证书校验'):system_ca_bundle()

    def test_verified_public_bundle_is_atomically_written_to_runner_project(self):
        value=trust_payload({});namespace={}
        exec(compile(REMOTE,'pip-trust-remote','exec'),namespace)
        def execute(args,**kwargs):
            self.assertEqual(args[:5],['docker','exec','-i','-w','/'])
            with patch.object(sys,'stdin',io.StringIO(kwargs['input'].decode())),patch.object(sys,'stdout',io.StringIO()):
                exec(compile(args[-1],'pip-trust-container','exec'),{})
            return types.SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as directory,patch.object(namespace['subprocess'],'run',side_effect=execute):
            path=namespace['prepare_pip_trust']({'base':directory,'container':'test-runner'},value)
            self.assertEqual(Path(path).read_text(),value['pem'])
            self.assertFalse(Path(path).with_suffix('.writing').exists())


if __name__=='__main__':unittest.main()
