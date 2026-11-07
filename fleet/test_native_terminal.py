import importlib.util
from pathlib import Path
import unittest


class NativeTerminalTests(unittest.TestCase):
    def module(self):
        spec=importlib.util.spec_from_file_location('native_terminal_test',Path(__file__).parent/'runner-overlay/scripts/aisbench_terminal.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_actual_terminal_screen_cells_are_forwarded_without_progress_reconstruction(self):
        emitted=[]
        screen,stream=self.module().terminal_screen(emitted.append,lambda _:None,100,10)
        stream.feed('\x1b[?1049h\x1b[2J\x1b[HTask Progress Table\r\n| Task | Progress |\r\n| gpqa | [####      ] 7/20 |')
        screen.snapshot(force=True)
        result=''.join(emitted)
        self.assertIn('| gpqa | [####      ] 7/20 |',result)
        self.assertNotIn('[AISBench progress:',result)
        emitted.clear()
        stream.feed('\x1b[?1049lHTTP 400 Bad Request\r\n')
        self.assertIn('HTTP 400 Bad Request\r\n',''.join(emitted))

    def test_regular_long_logs_are_not_reformatted_on_terminal_wrap(self):
        emitted=[]
        _,stream=self.module().terminal_screen(emitted.append,lambda _:None,10,5)
        native='HTTP 400 '+('x'*60)+'\r\n'
        stream.feed(native)
        self.assertEqual(''.join(emitted),native)
