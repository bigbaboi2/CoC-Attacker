"""Startup commands and MuMu detection never touch the real Run key in tests."""
import tempfile
import unittest
import winreg
from pathlib import Path
from unittest.mock import patch

from core import startup


class StartupPreferencesTests(unittest.TestCase):
    def test_windows_and_mumu_commands_quote_paths(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            scripts = Path(directory) / 'My Tool'
            scripts.mkdir()
            python = scripts / 'python.exe'
            pythonw = scripts / 'pythonw.exe'
            pythonw.touch()
            app = scripts / 'main.py'
            command = startup.startup_command('windows', python, app)
            self.assertIn('pythonw.exe', command)
            self.assertIn('"', command)
            self.assertNotIn('--wait-for-mumu', command)
            self.assertTrue(startup.startup_command('mumu', python, app).endswith('--wait-for-mumu'))
            self.assertEqual(startup.startup_command('off', python, app), '')

    def test_mumu_detection_uses_process_names(self):
        class Process:
            def __init__(self, name):
                self.info = {'name': name}

        self.assertTrue(startup.mumu_is_running([Process('MuMuNxDevice.exe')]))
        self.assertFalse(startup.mumu_is_running([Process('notepad.exe')]))
        with patch.object(startup, 'mumu_is_running', side_effect=[False, False, True]), \
                patch.object(startup.time, 'sleep') as sleep:
            startup.wait_for_mumu(interval=2)
            self.assertEqual(sleep.call_count, 2)

    def test_run_entry_is_only_written_for_enabled_modes(self):
        with patch.object(winreg, 'CreateKeyEx') as open_key, \
                patch.object(winreg, 'SetValueEx') as set_value, \
                patch.object(winreg, 'DeleteValue') as delete_value:
            startup.configure_startup('mumu')
            self.assertEqual(set_value.call_args.args[1], startup.RUN_VALUE)
            self.assertIn('--wait-for-mumu', set_value.call_args.args[4])
            startup.configure_startup('off')
            delete_value.assert_called_once_with(open_key.return_value.__enter__.return_value,
                                                 startup.RUN_VALUE)


if __name__ == '__main__':
    unittest.main()
