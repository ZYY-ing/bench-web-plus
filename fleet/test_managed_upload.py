import ast,base64,hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from .managed_ssh import ManagedSession


class ManagedUploadTests(unittest.TestCase):
    def test_large_archive_is_split_below_broker_limit_and_reassembled_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'large.tar.gz'
            with source.open('wb') as out:
                for n in range(34):out.write(bytes([n])*1024*1024)
            expected=hashlib.sha256(source.read_bytes()).hexdigest()
            session=ManagedSession({});received=bytearray();sizes=[];calls=[]
            def execute(code,**kwargs):
                calls.append(code);sizes.append(len(json.dumps({'script':base64.b64encode(code.encode()).decode()}).encode())+2048)
                self.assertNotIn('docker',code);self.assertNotIn('setup.sh',code)
                if len(calls)==1:
                    self.assertLess(len(code),4096)
                    return json.dumps({'cached':False,'temporary':f'/mnt/cache/ascend-bench-kit-{expected[:12]}.tar.gz.unique.upload'})
                if 'base64.b64decode' in code:
                    tree=ast.parse(code)
                    node=next(n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='loads')
                    args=json.loads(ast.literal_eval(node.args[0]));raw=base64.b64decode(args['data'],validate=True)
                    self.assertEqual(args['offset'],len(received));self.assertEqual(args['digest'],hashlib.sha256(raw).hexdigest());received.extend(raw)
                    return str(len(received))
                self.assertIn('os.replace(temporary,p)',code)
                self.assertEqual(hashlib.sha256(received).hexdigest(),expected)
                return 'uploaded'
            with patch.object(session,'execute',side_effect=execute):
                path=session.upload(source,'/mnt/cache')
            self.assertTrue(path.endswith(expected[:12]+'.tar.gz'))
            self.assertEqual(len(calls),36)
            self.assertLess(max(sizes),2*1024*1024)

    def test_cache_hit_sends_only_small_query(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'kit';source.write_bytes(b'archive-content')
            session=ManagedSession({})
            with patch.object(session,'execute',return_value='{"cached":true}') as execute:
                session.upload(source,'/mnt/cache')
            execute.assert_called_once();self.assertLess(len(execute.call_args.args[0]),4096)
            self.assertNotIn(base64.b64encode(source.read_bytes()).decode(),execute.call_args.args[0])

    def test_interrupted_chunk_is_not_replayed_or_published(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'kit';source.write_bytes(b'archive-content')
            digest=hashlib.sha256(source.read_bytes()).hexdigest();session=ManagedSession({})
            replies=[json.dumps({'cached':False,'temporary':f'/mnt/cache/ascend-bench-kit-{digest[:12]}.tar.gz.unique.upload'}),ValueError('result unknown')]
            with patch.object(session,'execute',side_effect=replies) as execute:
                with self.assertRaisesRegex(ValueError,'unknown'):session.upload(source,'/mnt/cache')
            self.assertEqual(execute.call_count,2)

    def test_oversized_management_script_is_rejected_before_connection(self):
        session=ManagedSession({})
        with patch.object(session,'connect') as connect:
            with self.assertRaisesRegex(ValueError,'远端未执行'):session.execute('x'*(24*1024*1024))
        connect.assert_not_called()
